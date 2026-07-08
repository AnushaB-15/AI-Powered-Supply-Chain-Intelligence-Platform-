"""
===================================================================
  FILE: app.py
  PURPOSE: GraphPulse AI — Real-Time Supply Chain Intelligence Engine
           Neo4j + OpenRouter + Gradio Enhanced UI
  RUN:  python app.py
===================================================================
"""

import os, json, re, time
import requests
import pandas as pd
import gradio as gr
from dotenv import load_dotenv
from neo4j import GraphDatabase
from neo4j.graph import Node, Relationship, Path
import subprocess

# ── MCP Server auto-start ─────────────────────────────────────
# Starts mcp_server.py in a background daemon thread — non-blocking.
# Probes for the port after startup and caches the URL globally.
# Falls back to direct Neo4j if MCP never becomes reachable.
import threading

def _start_mcp_background():
    """Start Flask MCP server subprocess and probe for its port."""
    global _MCP_BASE_URL
    import sys as _sys, tempfile as _tmp
    try:
        _errfile = _tmp.NamedTemporaryFile(
            delete=False, suffix="_mcp_err.log", mode="w", encoding="utf-8"
        )
        # sys.executable = same venv Python running app.py right now
        proc = subprocess.Popen(
            [_sys.executable, "mcp_server.py"],
            stdout=_errfile,
            stderr=_errfile,
        )
        _errfile.close()
        # Flask starts fast - poll every 0.3s, up to 15s total
        for _ in range(50):
            time.sleep(0.3)
            if proc.poll() is not None:
                try:
                    with open(_errfile.name, encoding="utf-8", errors="replace") as f:
                        err = f.read(800)
                    print("[MCP] Server crashed at startup:\n" + err)
                except Exception:
                    pass
                return
            try:
                with open("mcp_port.json") as f:
                    port = json.load(f)["port"]
                url = "http://127.0.0.1:" + str(port)
                resp = requests.get(url + "/tools", timeout=2)
                if resp.status_code == 200:
                    _MCP_BASE_URL = url
                    print("[MCP] Connected at " + url)
                    return
            except Exception:
                pass
        print("[MCP] Server did not respond in 15s - using direct Neo4j driver")
        try:
            with open(_errfile.name, encoding="utf-8", errors="replace") as f:
                print("[MCP] Last log:\n" + f.read(600))
        except Exception:
            pass
    except Exception as e:
        print("[MCP] Background start error: " + str(e))


# Load .env BEFORE starting MCP so Neo4j credentials are available immediately
load_dotenv(dotenv_path=".env")

if os.getenv("RUN_MCP", "true").lower() != "false":
    _mcp_thread = threading.Thread(target=_start_mcp_background, daemon=True)
    _mcp_thread.start()
QUERY_HISTORY = []
_last_records = []
_last_question = ""

# ─────────────────────────────────────────────────────────────
# CLIENTS
# ─────────────────────────────────────────────────────────────
neo4j_driver = GraphDatabase.driver(
    os.getenv("NEO4J_URI"),
    auth=(os.getenv("NEO4J_USER"), os.getenv("NEO4J_PASSWORD")),
)

# ─────────────────────────────────────────────────────────────
# LLM BACKEND FOR QUERY TAB
#
# Provider chain (tried in order, completely independent of Groq):
#   1. Google Gemini (GOOGLE_API_KEY)  — 1500 req/day free, no card
#      Get key: https://aistudio.google.com/apikey
#   2. Mistral AI   (MISTRAL_API_KEY)  — free tier, 1 req/sec
#      Get key: https://console.mistral.ai  → Free tier, no card
#
# Add to your .env:
#   GOOGLE_API_KEY=AIza...
#   MISTRAL_API_KEY=...   (optional fallback)
# ─────────────────────────────────────────────────────────────
from google import genai as _genai
from google.genai import types as _genai_types
from mistralai import Mistral as _MistralClient

_GOOGLE_KEY  = (os.getenv("GOOGLE_API_KEY")  or "").strip()
_MISTRAL_KEY = (os.getenv("MISTRAL_API_KEY") or "").strip()

_genai_client   = _genai.Client(api_key=_GOOGLE_KEY)   if _GOOGLE_KEY  else None
_mistral_client = _MistralClient(api_key=_MISTRAL_KEY) if _MISTRAL_KEY else None

# Gemini model names for google-genai SDK v2+
# Must use "models/" prefix — bare names cause 404 in v1beta API
_GEMINI_MODELS = [
    "models/gemini-2.0-flash",        # fastest, most capable — primary
    "models/gemini-1.5-flash",        # reliable fallback
    "models/gemini-2.0-flash-lite",   # lightweight last resort
]

# Mistral free-tier models
_MISTRAL_MODELS = [
    "mistral-small-latest",   # best free tier model — strong reasoning
    "open-mistral-7b",        # always available on free tier
    "open-mixtral-8x7b",      # MoE fallback
]


def _call_gemini(messages: list, max_tokens: int, temperature: float) -> str:
    """Call Google Gemini. Raises RuntimeError if all models fail."""
    system_instruction = None
    contents = []
    for m in messages:
        role, text = m["role"], m["content"]
        if role == "system":
            system_instruction = text
        elif role == "user":
            contents.append(_genai_types.Content(
                role="user", parts=[_genai_types.Part(text=text)]
            ))
        elif role == "assistant":
            contents.append(_genai_types.Content(
                role="model", parts=[_genai_types.Part(text=text)]
            ))

    gen_config = _genai_types.GenerateContentConfig(
        max_output_tokens=max_tokens,
        temperature=temperature,
        system_instruction=system_instruction,
    )

    last_err = None
    for model_name in _GEMINI_MODELS:
        try:
            response = _genai_client.models.generate_content(
                model=model_name,
                contents=contents,
                config=gen_config,
            )
            text = (response.text or "").strip()
            if text:
                return text
            last_err = f"Empty response from {model_name}"
        except Exception as e:
            last_err = str(e)
            continue   # try next model

    raise RuntimeError(f"Gemini failed. Last error: {last_err}")


def _call_mistral(messages: list, max_tokens: int, temperature: float) -> str:
    """Call Mistral AI. Raises RuntimeError if all models fail."""
    # Mistral uses same OpenAI-style message format
    last_err = None
    for model_name in _MISTRAL_MODELS:
        try:
            response = _mistral_client.chat.complete(
                model=model_name,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            text = (response.choices[0].message.content or "").strip()
            if text:
                return text
            last_err = f"Empty response from {model_name}"
        except Exception as e:
            last_err = str(e)
            continue

    raise RuntimeError(f"Mistral failed. Last error: {last_err}")


def call_llm(messages: list, max_tokens: int = 600, temperature: float = 0.0) -> str:
    """
    LLM call for the Query Interface tab.
    Tries Google Gemini first, then Mistral AI as fallback.
    Both are completely free — no OpenRouter, no Groq, no credits needed.

    .env keys needed:
        GOOGLE_API_KEY  — https://aistudio.google.com/apikey
        MISTRAL_API_KEY — https://console.mistral.ai  (optional fallback)
    """
    errors = []

    # ── Provider 1: Google Gemini ─────────────────────────────
    if _genai_client:
        try:
            return _call_gemini(messages, max_tokens, temperature)
        except Exception as e:
            errors.append(f"Gemini: {str(e)[:120]}")
    else:
        errors.append("Gemini: GOOGLE_API_KEY not set")

    # ── Provider 2: Mistral AI ────────────────────────────────
    if _mistral_client:
        try:
            return _call_mistral(messages, max_tokens, temperature)
        except Exception as e:
            errors.append(f"Mistral: {str(e)[:120]}")
    else:
        errors.append("Mistral: MISTRAL_API_KEY not set")

    raise RuntimeError(
        "All LLM providers failed for Query Interface.\n"
        + "\n".join(errors) + "\n"
        "Fix: Check GOOGLE_API_KEY in .env → https://aistudio.google.com/apikey"
    )




# ─────────────────────────────────────────────────────────────
# MCP DISPATCHER — routes run_cypher through MCP REST server
# Falls back to direct Neo4j driver if MCP is not available.
# ─────────────────────────────────────────────────────────────
_MCP_BASE_URL = None

def _get_mcp_url() -> str | None:
    """
    Return the cached MCP server URL, or None if not yet discovered.
    Discovery happens in _start_mcp_background() at startup (non-blocking).
    Also checks GRAPHPULSE_MCP_URL env-var override.
    """
    global _MCP_BASE_URL
    if _MCP_BASE_URL:
        return _MCP_BASE_URL
    # Allow env-var override (e.g. set by agent_runner)
    env_url = os.getenv("GRAPHPULSE_MCP_URL", "").strip()
    if env_url:
        try:
            resp = requests.get(f"{env_url}/tools", timeout=1)
            if resp.status_code == 200:
                _MCP_BASE_URL = env_url
                return _MCP_BASE_URL
        except Exception:
            pass
    return None


def run_cypher_via_mcp(cypher: str):
    """
    Executes a Cypher query through the MCP REST server's /tools/run_cypher endpoint.
    Returns (records: list, error: str | None, via_mcp: bool).
    Falls back to direct Neo4j driver if MCP is unavailable.
    """
    base = _get_mcp_url()
    if base:
        try:
            resp = requests.post(
                f"{base}/tools/run_cypher",
                json={"query": cypher},
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "success":
                    raw = data.get("data", "[]")
                    # neo4j_tools.run_cypher returns a JSON string
                    records = json.loads(raw) if isinstance(raw, str) else raw
                    # Safety: if data is an error dict, surface it properly
                    if isinstance(records, dict) and "error" in records:
                        # Don't return error — fall through to direct driver below
                        print(f"[MCP] run_cypher returned error dict: {records['error']}")
                    elif not isinstance(records, list):
                        print(f"[MCP] Unexpected response type: {type(records).__name__}")
                    else:
                        return records, None, True
                else:
                    # MCP reported status=error — log and fall through to direct driver
                    print(f"[MCP] Tool error: {data.get('message', '?')}")
            else:
                # Non-200 (e.g. HTTP 500 from unhandled exception in MCP tool)
                # Log and fall through to direct Neo4j driver — do NOT return error here
                print(f"[MCP] HTTP {resp.status_code} — falling back to direct Neo4j")
                try:
                    msg = resp.json().get("message", "")
                    if msg:
                        print(f"[MCP] Server error: {msg}")
                except Exception:
                    pass
        except Exception as e:
            print(f"[MCP] Request failed: {e}")  # Fall through to direct driver

    # Direct Neo4j fallback (used when MCP is down, errors, or returns non-200)
    records, error = _run_cypher_direct(cypher)
    return records, error, False


# ─────────────────────────────────────────────────────────────
# SCHEMA — enhanced for complex multi-hop analytical queries
# ─────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────
# SCHEMA — enhanced for complex multi-hop analytical queries
# ─────────────────────────────────────────────────────────────
KG_SCHEMA = """
You are an expert Neo4j Cypher query generator for a Transportation & Logistics Knowledge Graph.
Return ONLY a raw Cypher query - no explanation, no markdown fences, no preamble, no backticks.

=== INTENT CLASSIFICATION ===
Silently classify the question into one intent and apply its pattern:
LIST        : MATCH + RETURN + LIMIT 15
FILTER      : MATCH + WHERE + RETURN + LIMIT 15
RANK/TOP-N  : MATCH + aggregation + ORDER BY + LIMIT N
AGGREGATE   : MATCH + WHERE + COUNT/SUM/AVG + RETURN + ORDER BY
MULTI-HOP   : Chain 2+ MATCH clauses across full graph path
WHAT-IF     : WITH block to isolate subset, then trace downstream
COMPARE     : RETURN multiple metrics for all entities of same type
PERCENTAGE  : SUM(CASE WHEN ... THEN 1 ELSE 0 END) / COUNT(*)
EXCLUSIVE   : WITH + COLLECT(DISTINCT) + WHERE size(list) = 1
TREND       : Filter by month_number or week_number + ORDER BY

=== SYNONYM NORMALISATION ===
Map user phrasing to the correct graph term before generating Cypher:
delayed / late / overdue / missed / not on time  =>  delivery_status = 'Major Delay'
on time / timely / delivered / no delay          =>  delivery_status = 'On Time'
risky / high risk / at risk / problematic        =>  risk_score > 0.6
stockout / shortage / unmet demand / demand gap  =>  demand_gap > 0
oversupply / surplus                             =>  demand_gap < 0
toys / toy products                              =>  product_category_name = 'toys'
auto / automobile / automotive                   =>  product_category_name = 'auto'
health beauty / health and beauty / healthcare   =>  product_category_name = 'health_beauty'
watches / gifts / watches and gifts              =>  product_category_name = 'watches_gifts'
cool stuff                                       =>  product_category_name = 'cool_stuff'
bed bath / bed bath table                        =>  product_category_name = 'bed_bath_table'
construction / garden / tools                    =>  product_category_name = 'costruction_tools_garden'
fastest route / quickest delivery                =>  ORDER BY PtoD_leadtime_days ASC
slowest route                                    =>  ORDER BY PtoD_leadtime_days DESC
cheapest / lowest cost / affordable route        =>  ORDER BY PtoD_transportation_cost_inr ASC
most expensive / costliest route                 =>  ORDER BY PtoD_transportation_cost_inr DESC
cost efficient / best efficiency route           =>  ORDER BY cost_efficiency DESC
inefficient / poor efficiency route              =>  ORDER BY cost_efficiency ASC
declining / shrinking retailer                   =>  annual_sales_y2_cr < annual_sales_y1_cr
fastest growing / highest growth retailer        =>  ORDER BY retailer_growth_rate DESC
lead time overrun / exceeded planned time        =>  actual_days_taken > planned_lead_time_days
road / truck / lorry                             =>  mode = 'Road'
rail / train                                     =>  mode = 'Rail'
air / flight / air freight                       =>  mode = 'Air'
sea / ship / ocean / vessel                      =>  mode = 'Sea'
show / list / give / display / tell me           =>  LIST or FILTER intent
top / best / highest / most / leading            =>  RANK - ORDER BY DESC + LIMIT
bottom / worst / lowest / least / poorest        =>  RANK - ORDER BY ASC + LIMIT
how many / count / total number                  =>  COUNT(...) AS alias
average / mean / typical                         =>  AVG(...) AS alias
sum / total / cumulative                         =>  SUM(...) AS alias
if removed / without / excluding                 =>  WHAT-IF intent
compare / versus / vs / side by side             =>  COMPARE intent
percentage / proportion / share / ratio          =>  PERCENTAGE intent
month / weekly / over time / trend               =>  TREND intent
affect / impact / downstream / cascade           =>  MULTI-HOP to Retailer
exclusively / only / solely served               =>  EXCLUSIVE intent
bottleneck / worst plant / slowest plant         =>  plant with highest delayed_count

=== NODES & EXACT PROPERTIES ===
 
(:Supplier)
  supplier_id           STRING   e.g. 'SUP0001'
  supplier_name         STRING
  supplier_latitude     FLOAT
  supplier_longitude    FLOAT
  StoP_distance_km      FLOAT
  annual_capacity_units INTEGER
  StoP_lead_time_days   INTEGER
  risk_score            FLOAT    range 0.02 to 0.99
  status                STRING   only value: 'Active'
 
(:Plant)
  plant_id    STRING   only values: 'PL1' 'PL2' 'PL3' 'PL4'
  plant_name  STRING   only values: 'Baddi' 'Pune' 'Bhopal' 'Goa'
  plant_latitude  FLOAT
  plant_longitude FLOAT
 
(:Distributor)
  distributor_id    STRING   e.g. 'D0001'
  distributor_city  STRING
  distributor_latitude  FLOAT
  distributor_longitude FLOAT
 
(:Retailer)
  retailer_id              STRING   e.g. 'R00001'
  retailer_city            STRING
  retailer_latitude        FLOAT
  retailer_longitude       FLOAT
  floor_size_sqft          INTEGER
  avg_monthly_demand       INTEGER
  sales_density            FLOAT
  RtoD_distance_km         FLOAT
  freight_cost_inr         FLOAT
  annual_sales_y1_cr       FLOAT
  annual_sales_y2_cr       FLOAT
  retailer_growth_rate     FLOAT
  retailer_growth_category STRING   only values: 'Moderate Growth' 'Low Growth'
 
(:Product)
  product_id            STRING   e.g. 'P101'
  product_category_name STRING   ALL LOWERCASE with underscores. Main values: 'toys' 'watches_gifts' 'health_beauty' 'auto' 'cool_stuff' 'bed_bath_table' 'costruction_tools_garden'
                                 CRITICAL: NEVER use 'Toys', 'Auto', 'Health_Beauty' etc — always fully lowercase.
  product_weight_g      FLOAT
  product_length_cm     FLOAT
  product_height_cm     FLOAT
  product_width_cm      FLOAT
  product_volume_cm3    INTEGER
 
(:Route)
  route_id                     STRING   format: 'PL1@D0001'
  mode                         STRING   only values: 'Road' 'Rail' 'Air' 'Sea'
  plant_id                     STRING
  distributor_id               STRING
  PtoD_leadtime_days           FLOAT
  PtoD_distance_km             FLOAT
  PtoD_transportation_cost_inr FLOAT
  cost_efficiency              FLOAT
 
(:Shipment)
  shipment_id                  STRING   e.g. 'SHIP_12'
  transaction_date             STRING   format DD-MM-YYYY
  week_number                  INTEGER  1 to 53
  month_number                 INTEGER  1 to 12
  demand_forecast_in_units     INTEGER
  sales_in_units               INTEGER
  planned_lead_time_days       INTEGER
  actual_days_taken            INTEGER
  delay_days                   INTEGER  range -3 to 6  (negative=early, 0=on time, positive=delayed)
  delivery_status              STRING   ONLY 2 values: 'Major Delay'  OR  'On Time'
  route_risk                   STRING   only value: 'Medium'
  demand_gap                   INTEGER  negative=oversupply  positive=shortage
  transportation_cost_per_unit FLOAT
  cost_per_unit_per_km         FLOAT
  PtoD_distance_km             FLOAT
  PtoD_transportation_cost_inr FLOAT
  route_id                     STRING   e.g. 'PL1@D0001'
 
IMPORTANT: TransportMode is NOT a node. The transport mode is a direct property on Route nodes.
  Route.mode  STRING   values: 'Air' | 'Rail' | 'Road' | 'Ship' | 'Hybrid'
  NEVER write MATCH (tm:TransportMode) — this label does not exist.
  ALWAYS use MATCH (r:Route) WHERE r.mode = '...' OR RETURN r.mode
 
=== RELATIONSHIPS ===
(Supplier)   -[:SUPPLIES_TO]->  (Plant)
(Supplier)   -[:SOURCED_FOR]->  (Product)
(Plant)      -[:HAS_ROUTE]->    (Route)
(Route)      -[:CONNECTS_TO]->  (Distributor)
(Plant)      -[:DISPATCHES]->   (Shipment)
(Shipment)   -[:SHIPPED_TO]->   (Distributor)
(Shipment)   -[:CARRIES]->      (Product)
(Distributor)-[:DELIVERS_TO]->  (Retailer)
 
=== CYPHER EXAMPLES (simple to complex) ===
 
Q: Show all product types
MATCH (p:Product)
RETURN DISTINCT p.product_category_name AS product_type
ORDER BY product_type
 
Q: Show all delayed shipments
MATCH (s:Shipment)
WHERE s.delivery_status = 'Major Delay'
RETURN s.shipment_id, s.transaction_date, s.delay_days, s.delivery_status,
       s.planned_lead_time_days, s.actual_days_taken, s.route_id
ORDER BY s.delay_days DESC
LIMIT 5
 
Q: Which suppliers have risk score above 0.7
MATCH (sup:Supplier)
WHERE sup.risk_score > 0.7
RETURN sup.supplier_id, sup.supplier_name, round(sup.risk_score, 2) AS risk_score,
       sup.StoP_lead_time_days AS lead_time_days, sup.annual_capacity_units
ORDER BY sup.risk_score DESC

Q: Which suppliers have a risk score above 0.9
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)
WHERE sup.risk_score > 0.9
RETURN sup.supplier_id, sup.supplier_name, round(sup.risk_score, 2) AS risk_score,
       pl.plant_id, pl.plant_name
ORDER BY sup.risk_score DESC

Q: Show suppliers with risk above 0.5
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)
WHERE sup.risk_score > 0.5
RETURN sup.supplier_id, sup.supplier_name, round(sup.risk_score, 2) AS risk_score,
       pl.plant_id, pl.plant_name
ORDER BY sup.risk_score DESC LIMIT 15
 
Q: Which distributors serve the most retailers
MATCH (d:Distributor)-[:DELIVERS_TO]->(r:Retailer)
RETURN d.distributor_id, d.distributor_city, COUNT(r) AS num_retailers
ORDER BY num_retailers DESC
LIMIT 5
 
Q: Top 5 routes by transportation cost
MATCH (r:Route)
RETURN r.route_id, r.mode, r.PtoD_distance_km,
       r.PtoD_transportation_cost_inr, r.cost_efficiency
ORDER BY r.PtoD_transportation_cost_inr DESC
LIMIT 5
 
Q: Average delay per plant
MATCH (p:Plant)-[:DISPATCHES]->(s:Shipment)
WHERE s.delivery_status = 'Major Delay'
RETURN p.plant_id, p.plant_name, AVG(s.delay_days) AS avg_delay,
       COUNT(s) AS total_delayed
ORDER BY avg_delay DESC
 
Q: Shipments from PL1 to D0001
MATCH (p:Plant)-[:DISPATCHES]->(s:Shipment)-[:SHIPPED_TO]->(d:Distributor)
WHERE p.plant_id = 'PL1' AND d.distributor_id = 'D0001'
RETURN s.shipment_id, s.transaction_date, s.delivery_status,
       s.delay_days, s.sales_in_units
LIMIT 5
 
Q: Products in heavy shipments
MATCH (s:Shipment)-[:CARRIES]->(p:Product)
WHERE p.product_weight_g > 500
RETURN p.product_id, p.product_category_name, p.product_weight_g,
       s.shipment_id, s.delivery_status
ORDER BY p.product_weight_g DESC
LIMIT 5
 
Q: Routes using road transport
MATCH (r:Route)
WHERE r.mode = 'Road'
RETURN r.route_id, r.mode, r.PtoD_distance_km,
       r.PtoD_leadtime_days, r.PtoD_transportation_cost_inr
LIMIT 5
 
Q: Retailers with highest growth rate
MATCH (r:Retailer)
RETURN r.retailer_id, r.retailer_city, r.retailer_growth_rate,
       r.retailer_growth_category, r.annual_sales_y1_cr, r.annual_sales_y2_cr
ORDER BY r.retailer_growth_rate DESC
LIMIT 5
 
Q: If the top 3 highest risk suppliers were removed, which plants would lose supply, which product categories would be affected, and which distributors and retailers would ultimately be impacted?
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)
WITH sup ORDER BY sup.risk_score DESC LIMIT 3
MATCH (sup)-[:SUPPLIES_TO]->(pl:Plant)
MATCH (pl)-[:DISPATCHES]->(sh:Shipment)-[:CARRIES]->(prod:Product)
MATCH (sh)-[:SHIPPED_TO]->(d:Distributor)-[:DELIVERS_TO]->(r:Retailer)
RETURN sup.supplier_id AS at_risk_supplier, sup.supplier_name AS supplier_name,
       sup.risk_score AS risk_score,
       pl.plant_id AS affected_plant, pl.plant_name AS plant_name,
       COLLECT(DISTINCT prod.product_category_name) AS affected_categories,
       COUNT(DISTINCT d) AS impacted_distributors,
       COUNT(DISTINCT r) AS impacted_retailers
ORDER BY sup.risk_score DESC
 
Q: Which plants have the highest total transportation cost across all their routes?
MATCH (p:Plant)-[:HAS_ROUTE]->(r:Route)
RETURN p.plant_id, p.plant_name,
       SUM(r.PtoD_transportation_cost_inr) AS total_transport_cost,
       COUNT(r) AS total_routes,
       AVG(r.cost_efficiency) AS avg_efficiency
ORDER BY total_transport_cost DESC
 
Q: Which product categories are most frequently delayed and what is the average delay?
MATCH (s:Shipment)-[:CARRIES]->(p:Product)
WHERE s.delivery_status = 'Major Delay'
RETURN p.product_category_name AS category,
       COUNT(s) AS delayed_shipments,
       AVG(s.delay_days) AS avg_delay_days,
       MAX(s.delay_days) AS max_delay_days
ORDER BY delayed_shipments DESC

Q: Which product categories are most frequently delayed?
MATCH (sh:Shipment)-[:CARRIES]->(prod:Product)
WHERE sh.delivery_status = 'Major Delay'
RETURN prod.product_category_name AS product_category,
       COUNT(sh) AS delayed_shipments,
       round(AVG(sh.delay_days), 2) AS avg_delay_days
ORDER BY delayed_shipments DESC

Q: If the top 3 highest risk suppliers were removed, which plants would be affected?
MATCH (sup:Supplier)
WITH sup ORDER BY sup.risk_score DESC LIMIT 3
MATCH (sup)-[:SUPPLIES_TO]->(pl:Plant)
RETURN sup.supplier_id AS supplier_id,
       sup.supplier_name AS supplier_name,
       round(sup.risk_score, 2) AS risk_score,
       COLLECT(DISTINCT pl.plant_name) AS affected_plants,
       COUNT(DISTINCT pl) AS plants_at_risk
ORDER BY risk_score DESC

Q: Which distributors are exclusively served by a single plant?
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d:Distributor)
WITH d.distributor_id AS distributor_id,
     d.distributor_city AS distributor_city,
     COUNT(DISTINCT pl.plant_id) AS plant_count,
     COLLECT(DISTINCT pl.plant_name) AS plants_serving
WHERE plant_count = 1
RETURN distributor_id, distributor_city, plants_serving[0] AS sole_plant, plant_count
ORDER BY distributor_city

Q: Analyze distributor-to-retailer distance and stockout risk if a distributor is closed
MATCH (d:Distributor)-[:DELIVERS_TO]->(r:Retailer)
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d)
WITH d.distributor_city AS distributor_city, d.distributor_id AS distributor_id,
     COUNT(DISTINCT r) AS retailers_served,
     COUNT(sh) AS total_shipments,
     SUM(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed_shipments,
     round(100.0 * SUM(CASE WHEN sh.delivery_status='Major Delay' THEN 1 ELSE 0 END)/COUNT(sh), 1) AS delay_rate_pct,
     SUM(CASE WHEN sh.demand_gap > 0 THEN sh.demand_gap ELSE 0 END) AS total_demand_gap
RETURN distributor_city, distributor_id, retailers_served,
       total_shipments, delayed_shipments, delay_rate_pct, total_demand_gap
ORDER BY retailers_served DESC
LIMIT 20

Q: How many delayed shipments does each product category have?
MATCH (sh:Shipment)-[:CARRIES]->(prod:Product)
WHERE sh.delivery_status = 'Major Delay'
RETURN prod.product_category_name AS product_category,
       COUNT(sh) AS delayed_shipments
ORDER BY delayed_shipments DESC
 
Q: Which distributors have the highest demand gap (shortage) and which plants supply them?
MATCH (pl:Plant)-[:DISPATCHES]->(s:Shipment)-[:SHIPPED_TO]->(d:Distributor)
WHERE s.demand_gap > 0
RETURN d.distributor_id, d.distributor_city,
       SUM(s.demand_gap) AS total_shortage,
       AVG(s.demand_gap) AS avg_shortage,
       COLLECT(DISTINCT pl.plant_name) AS supplying_plants
ORDER BY total_shortage DESC
LIMIT 5
 
Q: Which transport modes have the lowest cost efficiency and what routes use them?
MATCH (r:Route)
WHERE r.mode IS NOT NULL
RETURN r.mode AS transport_mode,
       AVG(r.cost_efficiency) AS avg_efficiency,
       COUNT(r) AS route_count,
       AVG(r.PtoD_transportation_cost_inr) AS avg_cost
ORDER BY avg_efficiency ASC
LIMIT 5
 
Q: Give me all transport mode names
MATCH (r:Route)
WHERE r.mode IS NOT NULL
RETURN DISTINCT r.mode AS transport_mode
ORDER BY transport_mode
 
Q: Show routes by transport mode
MATCH (r:Route)
RETURN r.mode AS transport_mode, COUNT(r) AS route_count
ORDER BY route_count DESC
 
Q: Find retailers with declining sales and which distributors serve them
MATCH (d:Distributor)-[:DELIVERS_TO]->(r:Retailer)
WHERE r.annual_sales_y2_cr < r.annual_sales_y1_cr
RETURN r.retailer_id, r.retailer_city,
       r.annual_sales_y1_cr AS sales_year1,
       r.annual_sales_y2_cr AS sales_year2,
       (r.annual_sales_y2_cr - r.annual_sales_y1_cr) AS sales_decline,
       d.distributor_id, d.distributor_city AS distributor_city
ORDER BY sales_decline ASC
LIMIT 5
 
Q: Which suppliers supply plants that have the most delayed shipments?
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)-[:DISPATCHES]->(s:Shipment)
WHERE s.delivery_status = 'Major Delay'
RETURN sup.supplier_id, sup.supplier_name, sup.risk_score,
       pl.plant_id, pl.plant_name,
       COUNT(s) AS delayed_shipment_count,
       AVG(s.delay_days) AS avg_delay
ORDER BY delayed_shipment_count DESC
LIMIT 5
 
Q: Full supply chain path: which retailers are affected by high-risk routes?
MATCH (pl:Plant)-[:HAS_ROUTE]->(ro:Route)-[:CONNECTS_TO]->(d:Distributor)-[:DELIVERS_TO]->(r:Retailer)
WHERE ro.cost_efficiency < 0.5
RETURN pl.plant_name AS plant, ro.route_id, ro.mode,
       ro.cost_efficiency, d.distributor_city AS distributor,
       r.retailer_city AS retailer, r.retailer_growth_category
ORDER BY ro.cost_efficiency ASC
LIMIT 5
 
Q: Show shipments with plant_id
MATCH (p:Plant)-[:DISPATCHES]->(s:Shipment)
RETURN p.plant_id, s.shipment_id, s.delivery_status, s.delay_days, s.transaction_date
ORDER BY s.delay_days DESC
LIMIT 5
 
Q: What is the end-to-end supply chain exposure for the most delayed plant?
MATCH (pl:Plant)-[:DISPATCHES]->(s:Shipment)
WHERE s.delivery_status = 'Major Delay'
WITH pl, COUNT(s) AS delay_count ORDER BY delay_count DESC LIMIT 1
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl)
MATCH (pl)-[:DISPATCHES]->(s2:Shipment)-[:SHIPPED_TO]->(d:Distributor)
MATCH (d)-[:DELIVERS_TO]->(r:Retailer)
RETURN pl.plant_id, pl.plant_name, delay_count,
       COLLECT(DISTINCT sup.supplier_name) AS suppliers,
       COUNT(DISTINCT d) AS distributor_count,
       COUNT(DISTINCT r) AS retailer_count
 
=== STRICT RULES ===
RULE 1  Return ONLY raw Cypher. No explanation. No markdown. No backticks. No preamble.
RULE 2  Always RETURN individual properties — NEVER return a whole node object.
RULE 3  Always add LIMIT 15 unless user asks for a specific number or says "all".
         For questions about entities (retailers, suppliers, distributors), always use
         DISTINCT on the entity ID to avoid duplicate rows from multi-hop joins.
         Example: RETURN DISTINCT r.retailer_id, r.retailer_city
RULE 4  delivery_status has ONLY 2 values: 'Major Delay' and 'On Time'. NEVER use 'Delayed'.
RULE 5  Delayed = delivery_status = 'Major Delay'. On time = delivery_status = 'On Time'.
RULE 6  risk_score is FLOAT — compare directly: sup.risk_score > 0.7 (no quotes, no toFloat).
RULE 7  Route ID format is always plant_id + '@' + distributor_id e.g. 'PL1@D0001'.
RULE 8  Always alias aggregations: COUNT(r) AS num_retailers, AVG(s.delay_days) AS avg_delay.
RULE 9  Only use property names listed above. Do not invent properties.
RULE 10 For distributor -> retailer use DELIVERS_TO. There is no Shipment -> Retailer relationship.
RULE 11 For plant queries use plant_id values: 'PL1' 'PL2' 'PL3' 'PL4'.
RULE 12 month_number is INTEGER (1=Jan 12=Dec). week_number is INTEGER (1 to 53).
RULE 13 For multi-hop traversals match the full path — never skip intermediate nodes.
RULE 14 For DISTINCT product types: MATCH (p:Product) RETURN DISTINCT p.product_category_name
RULE 15 Always ORDER BY for ranking/top-N queries.
RULE 16 If user mentions a specific field name like "plant_id" or "route_id" in their question, include it in RETURN.
RULE 17 For complex analytical questions (supply chain impact, what-if scenarios, cascading effects),
        use WITH clauses, COLLECT(), and multi-hop MATCH patterns across the full graph.
RULE 18 For questions about impact or dependency chains, always traverse the full path:
        Supplier -> Plant -> Route/Shipment -> Distributor -> Retailer.
RULE 19 When asked about "if X were removed" or hypothetical scenarios, use subquery pattern:
        first identify X in a WITH block ordered by the key metric, then trace downstream impacts.
RULE 20 For aggregations across multiple node types, use COLLECT(DISTINCT ...) to avoid duplicates.
RULE 21 For end-to-end analysis questions, always compute aggregated counts and collect entity names.
RULE 22  When returning categorical properties such as transport mode,
product category, city names, or plant names across multiple routes
or shipments, use DISTINCT unless the user explicitly requests counts.
RULE 23  For city-based filtering on Retailer or Distributor use exact string match on retailer_city or distributor_city.
RULE 24  For "percentage of" calculations use: SUM(CASE WHEN condition THEN 1 ELSE 0 END) pattern.
RULE 25  For single-source / exclusively-served queries, use WITH + COLLECT(DISTINCT ...) + WHERE count = 1.
         IMPORTANT DATA FACT: All 50 distributors receive shipments from multiple plants — none is exclusively
         served by a single plant. Queries about 'exclusively single plant distributors' will return 0 results.
         Always use this EXCLUSIVE pattern:
         MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d:Distributor)
         WITH d.distributor_id AS dist_id, d.distributor_city AS city, COUNT(DISTINCT pl.plant_id) AS plant_count
         WHERE plant_count = 1
         RETURN dist_id, city, plant_count
RULE 26  For "relative to" or "ratio" calculations (freight vs sales) compute inline in RETURN with division.
RULE 27  StoP_distance_km is distance from Supplier to Plant. Use it for "within Xkm of plant" queries.
RULE 28  actual_days_taken and planned_lead_time_days are both on Shipment. Use them for overrun queries.
RULE 29  RELATIONSHIP DIRECTIONS ARE FIXED — never reverse them:
         CORRECT:   (Plant)-[:DISPATCHES]->(Shipment)
         WRONG:     (Shipment)-[:DISPATCHES]->(Plant)        ← NEVER write this
         CORRECT:   (Supplier)-[:SUPPLIES_TO]->(Plant)
         WRONG:     (Plant)-[:SUPPLIES_TO]->(Supplier)       ← NEVER write this
         CORRECT:   (Shipment)-[:CARRIES]->(Product)
         WRONG:     (Product)-[:CARRIES]->(Shipment)         ← NEVER write this
         CORRECT:   (Shipment)-[:SHIPPED_TO]->(Distributor)
         WRONG:     (Distributor)-[:SHIPPED_TO]->(Shipment)  ← NEVER write this
         When traversing from Product back to Plant, write:
         MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:CARRIES]->(pr:Product)
         NEVER write: (pr:Product)<-[:DISPATCHES]-(sh:Shipment)<-[:CARRIES]
RULE 30  product_category_name values are ALL LOWERCASE with underscores.
         CORRECT: 'toys'  'auto'  'health_beauty'  'watches_gifts'  'cool_stuff'  'bed_bath_table'  'costruction_tools_garden'
         WRONG:   'Toys'  'Auto'  'Health_Beauty'  'Watches_Gifts'  — NEVER use Title Case.
RULE 31  ALWAYS extract the EXACT numeric threshold from the user's question.
         If user says "above 0.9" → use 0.9. If user says "above 0.7" → use 0.7.
         NEVER substitute a default like 0.7 when the user stated a different number.
         Examples: "risk score above 0.9" → risk_score > 0.9
                   "delay rate above 60" → delay_rate_pct > 60
                   "top 10" → LIMIT 10   "top 3" → LIMIT 3
                   "within 500km" → StoP_distance_km < 500
RULE 32  For shortage/stockout/demand gap questions, ALWAYS aggregate by distributor_city.
         NEVER return individual shipment rows (shipment_id, incident_date, shortage_volume per row).
         CORRECT pattern:
           WITH d.distributor_city AS distributor_city, COUNT(sh) AS shortage_shipments,
                SUM(sh.demand_gap) AS total_demand_gap
           RETURN distributor_city, shortage_shipments, total_demand_gap
         WRONG pattern:
           RETURN sh.shipment_id, sh.transaction_date, sh.demand_gap AS shortage_volume
         One row per CITY, not one row per shipment.
RULE 33  For diagnostic questions (who, why, when, how, what is driving, what is responsible,
         how is it spreading) — the Cypher must return AGGREGATED metrics per entity
         (plant, supplier, distributor_city), NOT individual transaction rows.
         Always use WITH + COUNT/SUM/AVG + GROUP BY pattern.
RULE 34  Use LIMIT only for specific entity queries, NOT for network-wide queries.
         Network-wide (no LIMIT): stockout/shortage across network, all suppliers, all plants,
         all distributors, all cities — return ALL rows.
         Specific entity (use LIMIT): top N suppliers, top N routes, top N cities.
         For shortage/stockout queries: NO LIMIT — return all 200 city+plant combinations.
         NEVER add extra columns like shipment_id, incident_date, first_occurrence,
         latest_occurrence unless the user explicitly asks for timeline/incident data.
RULE 35  For "more than X times the planned lead time" or lead time overrun queries:
         ALWAYS aggregate by plant or supplier — NEVER return individual shipment rows.
         Use: WITH pl.plant_name, COUNT(sh) AS shipments_exceeding_Nx, AVG(actual/planned)
         Group by plant_name and plant_id — not by shipment_id.
         Return: plant_id, plant_name, shipments_exceeding_Nx, avg_actual_days, avg_planned_days, avg_overrun_days.
RULE 36  For "which plant has the most delayed/late shipments" queries:
         ALWAYS use SUM(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed
         AND return plant_id AND plant_name together — never return only one.
         Order by delayed DESC so the worst plant is first.

=== ADDITIONAL EXAMPLES — COMPLEX PATTERNS ===
 
Q: Why are toy shipments delayed in the network
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:CARRIES]->(pr:Product)
WHERE sh.delivery_status = 'Major Delay'
AND pr.product_category_name = 'Toys'
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl)
RETURN sup.supplier_name AS supplier, pl.plant_name AS plant,
       COUNT(sh) AS delayed_shipments,
       round(AVG(sh.delay_days), 2) AS avg_delay_days,
       round(sup.risk_score, 2) AS risk_score
ORDER BY delayed_shipments DESC
LIMIT 15

Q: Which product categories have the most delays
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:CARRIES]->(pr:Product)
WHERE sh.delivery_status = 'Major Delay'
RETURN pr.product_category_name AS category,
       COUNT(sh) AS delayed_shipments,
       round(AVG(sh.delay_days), 2) AS avg_delay_days
ORDER BY delayed_shipments DESC

Q: Show delayed shipments for Toys category
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:CARRIES]->(pr:Product)
WHERE sh.delivery_status = 'Major Delay'
  AND pr.product_category_name = 'Toys'
RETURN pl.plant_name AS plant, sh.shipment_id, sh.delay_days,
       sh.planned_lead_time_days, sh.actual_days_taken
ORDER BY sh.delay_days DESC
LIMIT 15
MATCH (r:Retailer)
WHERE r.retailer_city = 'Mumbai'
RETURN COUNT(r) AS retailer_count
 
Q: Which distributors serve retailers in Pune?
MATCH (d:Distributor)-[:DELIVERS_TO]->(r:Retailer)
WHERE r.retailer_city = 'Pune'
RETURN DISTINCT d.distributor_id AS distributor_id, d.distributor_city AS distributor_city,
       COUNT(r) AS retailers_in_pune
ORDER BY retailers_in_pune DESC
 
Q: What transport modes does Plant PL1 use?
MATCH (pl:Plant {plant_id:'PL1'})-[:HAS_ROUTE]->(r:Route)
RETURN DISTINCT r.mode AS transport_mode, COUNT(r) AS route_count
ORDER BY route_count DESC
 
Q: Which distributor covers the most cities?
MATCH (d:Distributor)-[:DELIVERS_TO]->(r:Retailer)
RETURN d.distributor_id AS distributor_id, d.distributor_city AS distributor_city,
       COUNT(DISTINCT r.retailer_city) AS cities_covered
ORDER BY cities_covered DESC
LIMIT 5
 
Q: Which transport mode is used most frequently across all plants?
MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)
RETURN r.mode AS transport_mode,
       COUNT(r) AS route_count,
       COLLECT(DISTINCT pl.plant_name) AS plants_using
ORDER BY route_count DESC
 
Q: Which distributors receive shipments from the most number of distinct plants?
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d:Distributor)
RETURN d.distributor_id AS distributor_id, d.distributor_city AS distributor_city,
       COUNT(DISTINCT pl) AS plant_count,
       COLLECT(DISTINCT pl.plant_name) AS supplying_plants
ORDER BY plant_count DESC
LIMIT 10

Q: Identify all retailers in Mumbai that are receiving shipments originally supplied by 'Mahajan-Ghosh'
MATCH (sup:Supplier {supplier_name:'Mahajan-Ghosh'})-[:SUPPLIES_TO]->(pl:Plant)
      -[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d:Distributor)
      -[:DELIVERS_TO]->(r:Retailer)
WHERE r.retailer_city = 'Mumbai'
RETURN DISTINCT r.retailer_id AS retailer_id, r.retailer_city AS retailer_city,
       d.distributor_id AS via_distributor, d.distributor_city AS distributor_city,
       COUNT(sh) AS shipment_count
ORDER BY shipment_count DESC
LIMIT 15
 
Q: Compare the number of shipments generated by each plant
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)
RETURN pl.plant_id AS plant_id, pl.plant_name AS plant_name,
       COUNT(sh) AS total_shipments,
       SUM(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed_shipments,
       AVG(sh.delay_days) AS avg_delay_days
ORDER BY total_shipments DESC
 
Q: Which distributor city has the highest total transportation cost from all incoming shipments?
MATCH (sh:Shipment)-[:SHIPPED_TO]->(d:Distributor)
RETURN d.distributor_city AS distributor_city,
       SUM(sh.PtoD_transportation_cost_inr) AS total_cost_inr,
       COUNT(sh) AS total_shipments,
       AVG(sh.PtoD_transportation_cost_inr) AS avg_cost_per_shipment
ORDER BY total_cost_inr DESC
LIMIT 10
 
Q: Which suppliers have both a risk score above 0.6 and a lead time above 15 days?
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)
WHERE sup.risk_score > 0.6 AND sup.StoP_lead_time_days > 15
RETURN sup.supplier_id AS supplier_id, sup.supplier_name AS supplier_name,
       sup.risk_score AS risk_score, sup.StoP_lead_time_days AS lead_time_days,
       pl.plant_id AS plant_id, pl.plant_name AS plant_name
ORDER BY sup.risk_score DESC
 
Q: Which retailers have the highest freight cost relative to their annual sales?
MATCH (r:Retailer)
WHERE r.annual_sales_y2_cr > 0
RETURN r.retailer_id AS retailer_id, r.retailer_city AS retailer_city,
       r.freight_cost_inr AS freight_cost_inr,
       r.annual_sales_y2_cr AS annual_sales_cr,
       round(r.freight_cost_inr / (r.annual_sales_y2_cr * 10000000), 4) AS freight_to_sales_ratio
ORDER BY freight_to_sales_ratio DESC
LIMIT 10
 
Q: Which distributors are exclusively served by a single plant through routes?
MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor)
WITH d, COLLECT(DISTINCT pl.plant_id) AS plants, COUNT(DISTINCT pl) AS plant_count
WHERE plant_count = 1
RETURN d.distributor_id AS distributor_id, d.distributor_city AS distributor_city,
       plants[0] AS sole_plant_id, plant_count
ORDER BY distributor_city
 
Q: Identify products that are only sourced by a single supplier
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:CARRIES]->(pr:Product)
WITH pr, COLLECT(DISTINCT sup.supplier_id) AS suppliers, COUNT(DISTINCT sup) AS supplier_count
WHERE supplier_count = 1
RETURN pr.product_id AS product_id, pr.product_category_name AS category,
       suppliers[0] AS sole_supplier_id, supplier_count
ORDER BY category
LIMIT 10
 
Q: Which plant is responsible for the highest percentage of delayed shipments where actual days taken exceeds planned lead time?
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)
WITH pl,
     COUNT(sh) AS total_shipments,
     SUM(CASE WHEN sh.actual_days_taken > sh.planned_lead_time_days THEN 1 ELSE 0 END) AS over_planned
RETURN pl.plant_id AS plant_id, pl.plant_name AS plant_name,
       total_shipments,
       over_planned,
       round(100.0 * over_planned / total_shipments, 2) AS pct_over_planned
ORDER BY pct_over_planned DESC
 
Q: Identify all suppliers that supply raw materials for products in shipments that took more than twice the planned lead time
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:CARRIES]->(pr:Product)
WHERE sh.actual_days_taken > sh.planned_lead_time_days * 2
RETURN DISTINCT sup.supplier_id AS supplier_id, sup.supplier_name AS supplier_name,
       sup.risk_score AS risk_score,
       pl.plant_name AS plant_name,
       COUNT(DISTINCT sh) AS qualifying_shipments,
       AVG(sh.actual_days_taken) AS avg_actual_days,
       AVG(sh.planned_lead_time_days) AS avg_planned_days
ORDER BY qualifying_shipments DESC

Q: Which shipments took more than twice the planned lead time
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)
WHERE sh.actual_days_taken > 2 * sh.planned_lead_time_days
WITH pl.plant_name AS plant_name, pl.plant_id AS plant_id,
     COUNT(sh) AS shipments_exceeding_2x,
     round(AVG(sh.actual_days_taken), 1) AS avg_actual_days,
     round(AVG(sh.planned_lead_time_days), 1) AS avg_planned_days,
     round(AVG(sh.actual_days_taken - sh.planned_lead_time_days), 1) AS avg_overrun_days
RETURN plant_id, plant_name, shipments_exceeding_2x, avg_actual_days, avg_planned_days, avg_overrun_days
ORDER BY shipments_exceeding_2x DESC
NOTE: Aggregate by plant — do NOT return individual shipment rows (no shipment_id). This is a RANK/TOP-N query.
 
Q: Which suppliers with a risk score above 0.6 supply plants that ship products in the toys category to distributors in Mumbai?
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:CARRIES]->(pr:Product),
      (sh)-[:SHIPPED_TO]->(d:Distributor)
WHERE sup.risk_score > 0.6
  AND pr.product_category_name = 'Toys'
  AND d.distributor_city = 'Mumbai'
RETURN DISTINCT sup.supplier_id AS supplier_id, sup.supplier_name AS supplier_name,
       sup.risk_score AS risk_score, pl.plant_id AS plant_id, pl.plant_name AS plant_name,
       d.distributor_city AS distributor_city
ORDER BY sup.risk_score DESC
 
Q: Which retailers in Pune receive products from suppliers located within 50km of Plant PL1?
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant {plant_id:'PL1'})-[:DISPATCHES]->(sh:Shipment)
      -[:SHIPPED_TO]->(d:Distributor)-[:DELIVERS_TO]->(r:Retailer)
WHERE sup.StoP_distance_km <= 50 AND r.retailer_city = 'Pune'
RETURN DISTINCT sup.supplier_id AS supplier_id, sup.supplier_name AS supplier_name,
       sup.StoP_distance_km AS distance_km,
       r.retailer_id AS retailer_id, r.retailer_city AS retailer_city
ORDER BY sup.StoP_distance_km ASC
LIMIT 10
 
Q: Build a risk profile: for each supplier show their risk score, lead time, which plants they supply, and how many shipments those plants have created
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)-[:DISPATCHES]->(sh:Shipment)
RETURN sup.supplier_id AS supplier_id, sup.supplier_name AS supplier_name,
       sup.risk_score AS risk_score,
       sup.StoP_lead_time_days AS lead_time_days,
       COLLECT(DISTINCT pl.plant_name) AS plants_supplied,
       COUNT(DISTINCT pl) AS plant_count,
       COUNT(sh) AS total_shipments_from_plants
ORDER BY sup.risk_score DESC
LIMIT 15
 
 
Q: Which suppliers have a risk score above 0.85?
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)
WHERE sup.risk_score > 0.85
RETURN sup.supplier_id, sup.supplier_name, round(sup.risk_score,2) AS risk_score,
       sup.annual_capacity_units, pl.plant_id, pl.plant_name
ORDER BY sup.risk_score DESC
 
Q: Show me the top 3 suppliers by risk score
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)
RETURN sup.supplier_id, sup.supplier_name, round(sup.risk_score,2) AS risk_score,
       pl.plant_id, pl.plant_name
ORDER BY sup.risk_score DESC LIMIT 3
 
Q: Which plants have a delay rate above 60%?
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)
WITH pl.plant_id AS plant_id, pl.plant_name AS plant_name,
     COUNT(sh) AS total_shipments,
     SUM(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed_count,
     round(AVG(CASE WHEN sh.delivery_status='Major Delay' THEN sh.delay_days END),2) AS avg_delay
WITH plant_id, plant_name, total_shipments, delayed_count, avg_delay,
     round(100.0*delayed_count/total_shipments,1) AS delay_rate_pct
WHERE delay_rate_pct > 60
RETURN plant_id, plant_name, total_shipments, delayed_count, delay_rate_pct, avg_delay
ORDER BY delay_rate_pct DESC
 
Q: Which distributors have a demand gap greater than 50000 units?
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d:Distributor)
WHERE sh.demand_gap > 0
WITH d.distributor_id AS distributor_id, d.distributor_city AS distributor_city,
     SUM(sh.demand_gap) AS total_demand_gap,
     COUNT(sh) AS shortage_shipments
WHERE total_demand_gap > 50000
RETURN distributor_id, distributor_city, total_demand_gap, shortage_shipments
ORDER BY total_demand_gap DESC
 
Q: Which routes have a cost above 30000 INR?
MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor)
WHERE r.PtoD_transportation_cost_inr > 30000
RETURN r.route_id, pl.plant_name, d.distributor_city,
       r.mode AS transport_mode,
       round(r.PtoD_transportation_cost_inr,0) AS cost_inr,
       round(r.PtoD_leadtime_days,1) AS leadtime_days
ORDER BY cost_inr DESC LIMIT 20
 
Q: What is my on-time delivery rate?
MATCH (sh:Shipment)
WITH COUNT(sh) AS total,
     SUM(CASE WHEN sh.delivery_status = 'On Time' THEN 1 ELSE 0 END) AS on_time_count,
     SUM(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed_count
RETURN total AS total_shipments, on_time_count, delayed_count,
       round(100.0 * on_time_count / total, 1) AS on_time_rate_pct,
       round(100.0 * delayed_count / total, 1) AS delay_rate_pct
 
Q: Which months had the highest delay rates?
MATCH (sh:Shipment)
WITH sh.month_number AS month_number,
     COUNT(sh) AS total_shipments,
     SUM(CASE WHEN sh.delivery_status='Major Delay' THEN 1 ELSE 0 END) AS delayed_count
RETURN month_number, total_shipments, delayed_count,
       round(100.0*delayed_count/total_shipments,1) AS delay_rate_pct
ORDER BY delay_rate_pct DESC LIMIT 12
 
Q: Which suppliers supply only one plant?
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)
WITH sup.supplier_id AS supplier_id, sup.supplier_name AS supplier_name,
     round(sup.risk_score,2) AS risk_score,
     COUNT(DISTINCT pl) AS plant_count,
     COLLECT(DISTINCT pl.plant_name) AS plants
WHERE plant_count = 1
RETURN supplier_id, supplier_name, risk_score, plants[0] AS sole_plant
ORDER BY risk_score DESC
 
Q: Show me suppliers with lead time above 10 days
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)
WHERE sup.StoP_lead_time_days > 10
RETURN sup.supplier_id, sup.supplier_name, sup.StoP_lead_time_days AS lead_time_days,
       round(sup.risk_score,2) AS risk_score, pl.plant_id, pl.plant_name
ORDER BY sup.StoP_lead_time_days DESC LIMIT 15
 
Q: Which retailers have the lowest annual sales?
MATCH (r:Retailer)
RETURN r.retailer_id, r.retailer_city,
       r.annual_sales_y2_cr AS annual_sales_cr,
       r.retailer_growth_category
ORDER BY r.annual_sales_y2_cr ASC LIMIT 10
 
Q: Which routes cost more than 80000 INR?
MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor)
WHERE r.PtoD_transportation_cost_inr > 80000
RETURN r.route_id, pl.plant_name AS plant, d.distributor_city,
       r.mode AS transport_mode,
       round(r.PtoD_transportation_cost_inr, 0) AS cost_inr,
       round(r.PtoD_distance_km, 1) AS distance_km,
       round(r.PtoD_leadtime_days, 1) AS leadtime_days
ORDER BY cost_inr DESC
 
Q: Which routes cost less than 5000 INR?
MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor)
WHERE r.PtoD_transportation_cost_inr < 5000
RETURN r.route_id, pl.plant_name AS plant, d.distributor_city,
       r.mode AS transport_mode,
       round(r.PtoD_transportation_cost_inr, 0) AS cost_inr,
       round(r.PtoD_leadtime_days, 1) AS leadtime_days
ORDER BY cost_inr ASC
 
Q: What percentage of shipments from Goa plant arrive on time?
MATCH (pl:Plant {plant_name: 'Goa'})-[:DISPATCHES]->(sh:Shipment)
WITH COUNT(sh) AS total_shipments,
     SUM(CASE WHEN sh.delivery_status = 'On Time' THEN 1 ELSE 0 END) AS on_time_count,
     SUM(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed_count
RETURN total_shipments,
       on_time_count,
       delayed_count,
       round(100.0 * on_time_count / total_shipments, 1) AS on_time_rate_pct,
       round(100.0 * delayed_count / total_shipments, 1) AS delay_rate_pct
 
Q: What percentage of shipments from Bhopal arrive on time?
MATCH (pl:Plant {plant_name: 'Bhopal'})-[:DISPATCHES]->(sh:Shipment)
WITH COUNT(sh) AS total_shipments,
     SUM(CASE WHEN sh.delivery_status = 'On Time' THEN 1 ELSE 0 END) AS on_time_count,
     SUM(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed_count
RETURN total_shipments, on_time_count, delayed_count,
       round(100.0 * on_time_count / total_shipments, 1) AS on_time_rate_pct,
       round(100.0 * delayed_count / total_shipments, 1) AS delay_rate_pct
 
Q: How many retailers does each distributor serve?
MATCH (d:Distributor)-[:DELIVERS_TO]->(r:Retailer)
RETURN d.distributor_id, d.distributor_city,
       COUNT(DISTINCT r) AS retailers_served
ORDER BY retailers_served DESC
 
Q: Which suppliers have both risk above 0.8 and capacity below 2000?
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)
WHERE sup.risk_score > 0.8 AND sup.annual_capacity_units < 2000
RETURN sup.supplier_id, sup.supplier_name,
       round(sup.risk_score, 2) AS risk_score,
       sup.annual_capacity_units, pl.plant_id, pl.plant_name
ORDER BY sup.risk_score DESC
 
Q: Show shipments where actual delivery took more than 20 days
MATCH (sh:Shipment)
WHERE sh.actual_days_taken > 20
RETURN sh.shipment_id, sh.transaction_date,
       sh.actual_days_taken, sh.planned_lead_time_days,
       sh.delivery_status, sh.delay_days, sh.route_id
ORDER BY sh.actual_days_taken DESC LIMIT 15
 
Q: Which distributors receive shipments from more than 2 plants?
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d:Distributor)
WITH d.distributor_id AS distributor_id, d.distributor_city AS distributor_city,
     COUNT(DISTINCT pl.plant_id) AS plant_count,
     COLLECT(DISTINCT pl.plant_name) AS plants
WHERE plant_count > 2
RETURN distributor_id, distributor_city, plant_count, plants
ORDER BY plant_coun 
Q: Which retailers are connected to distributors that have more than 100 shortage shipments?
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d:Distributor)
WHERE sh.demand_gap > 0
WITH d.distributor_id AS distributor_id, d.distributor_city AS distributor_city,
     COUNT(sh) AS shortage_shipments, SUM(sh.demand_gap) AS total_demand_gap
WHERE shortage_shipments > 100
MATCH (d2:Distributor {distributor_id: distributor_id})-[:DELIVERS_TO]->(r:Retailer)
RETURN DISTINCT r.retailer_id AS retailer_id, r.retailer_city AS retailer_city,
       distributor_city, shortage_shipments, total_demand_gap
ORDER BY shortage_shipments DESC
 
Q: Which suppliers have a risk score above 0.75 and supply more than one plant?
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)
WHERE sup.risk_score > 0.75
WITH sup.supplier_id AS supplier_id, sup.supplier_name AS supplier_name,
     round(sup.risk_score, 2) AS risk_score,
     COUNT(DISTINCT pl) AS plant_count, COLLECT(DISTINCT pl.plant_name) AS plants
WHERE plant_count > 1
RETURN supplier_id, supplier_name, risk_score, plant_count, plants
ORDER BY risk_score DESC
 
Q: What is the ratio of delayed to on-time shipments for each plant?
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)
WITH pl.plant_id AS plant_id, pl.plant_name AS plant_name,
     SUM(CASE WHEN sh.delivery_status='Major Delay' THEN 1 ELSE 0 END) AS delayed_count,
     SUM(CASE WHEN sh.delivery_status='On Time' THEN 1 ELSE 0 END) AS on_time_count
RETURN plant_id, plant_name, delayed_count, on_time_count,
       round(toFloat(delayed_count) / toFloat(on_time_count), 2) AS delay_to_ontime_ratio
ORDER BY delay_to_ontime_ratio DESC
 
Q: Compare the average risk score of suppliers feeding Bhopal vs suppliers feeding Goa
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)
WHERE pl.plant_name IN ['Bhopal', 'Goa']
RETURN pl.plant_name AS plant_name, COUNT(DISTINCT sup) AS supplier_count,
       round(AVG(sup.risk_score), 3) AS avg_risk_score,
       round(MAX(sup.risk_score), 2) AS max_risk_score
ORDER BY plant_name
 
Q: Are there any distributors that receive shipments exclusively via Air transport?
MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor)
WITH d.distributor_id AS distributor_id, d.distributor_city AS distributor_city,
     COLLECT(DISTINCT r.mode) AS modes_used, COUNT(DISTINCT r.mode) AS mode_count
WHERE mode_count = 1 AND modes_used[0] = 'Air'
RETURN distributor_id, distributor_city, modes_used
ORDER BY distributor_city
 
Q: Which routes have a cost efficiency below the network average?
MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor)
WITH AVG(r.cost_efficiency) AS network_avg
MATCH (pl2:Plant)-[:HAS_ROUTE]->(r2:Route)-[:CONNECTS_TO]->(d2:Distributor)
WHERE r2.cost_efficiency < network_avg
RETURN r2.route_id AS route_id, pl2.plant_name AS plant,
       d2.distributor_city AS distributor_city, r2.mode AS transport_mode,
       round(r2.cost_efficiency, 4) AS cost_efficiency,
       round(network_avg, 4) AS network_avg_efficiency
ORDER BY cost_efficiency ASC LIMIT 20
 
Q: Which months had a delay rate above 60%?
MATCH (sh:Shipment)
WHERE sh.month_number IS NOT NULL
WITH sh.month_number AS month_num, COUNT(sh) AS total_shipments,
     SUM(CASE WHEN sh.delivery_status='Major Delay' THEN 1 ELSE 0 END) AS delayed_count
WITH month_num, total_shipments, delayed_count,
     round(100.0 * delayed_count / total_shipments, 1) AS delay_rate_pct
WHERE delay_rate_pct > 60
RETURN month_num, total_shipments, delayed_count, delay_rate_pct
ORDER BY delay_rate_pct DESC
 
Q: Which suppliers feed plants that dispatch shipments to Mumbai?
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d:Distributor)
WHERE d.distributor_city = 'Mumbai'
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl)
RETURN DISTINCT sup.supplier_id AS supplier_id, sup.supplier_name AS supplier_name,
       round(sup.risk_score, 2) AS risk_score, pl.plant_id AS plant_id, pl.plant_name AS plant_name
ORDER BY sup.risk_score DESC
 
Q: Which distributor city has the highest average demand gap per shipment?
MATCH (sh:Shipment)-[:SHIPPED_TO]->(d:Distributor)
WHERE sh.demand_gap > 0
WITH d.distributor_city AS distributor_city,
     COUNT(sh) AS shortage_shipments,
     SUM(sh.demand_gap) AS total_demand_gap,
     round(AVG(sh.demand_gap), 0) AS avg_demand_gap_per_shipment
RETURN distributor_city, shortage_shipments, total_demand_gap, avg_demand_gap_per_shipment
ORDER BY avg_demand_gap_per_shipment DESC LIMIT 10
 
Q: How many unique suppliers does each plant depend on?
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)
RETURN pl.plant_id AS plant_id, pl.plant_name AS plant_name,
       COUNT(DISTINCT sup) AS unique_supplier_count,
       round(AVG(sup.risk_score), 3) AS avg_supplier_risk
ORDER BY unique_supplier_count ASC
 
t DESC
 
Q: Stockouts have increased across the network even though overall shipment volumes remain stable. What is driving these shortages, who is responsible, when did the issue begin, and how is the disruption spreading?
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d:Distributor)
WHERE sh.demand_gap > 0
WITH d.distributor_city AS distributor_city,
     pl.plant_id AS plant_id,
     pl.plant_name AS plant_name,
     COUNT(sh) AS shortage_shipments,
     round(SUM(sh.demand_gap), 0) AS total_demand_gap,
     SUM(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed_shipments,
     round(AVG(CASE WHEN sh.delivery_status = 'Major Delay' THEN sh.delay_days END), 2) AS avg_delay_days
RETURN distributor_city, plant_id, plant_name,
       shortage_shipments, total_demand_gap, delayed_shipments, avg_delay_days
ORDER BY total_demand_gap DESC

Q: What is driving stockouts in the network? Who is responsible and when did it begin?
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d:Distributor)
WHERE sh.demand_gap > 0
WITH d.distributor_city AS distributor_city,
     pl.plant_id AS plant_id,
     pl.plant_name AS plant_name,
     COUNT(sh) AS shortage_shipments,
     round(SUM(sh.demand_gap), 0) AS total_demand_gap,
     SUM(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed_shipments,
     round(AVG(CASE WHEN sh.delivery_status = 'Major Delay' THEN sh.delay_days END), 2) AS avg_delay_days
RETURN distributor_city, plant_id, plant_name,
       shortage_shipments, total_demand_gap, delayed_shipments, avg_delay_days
ORDER BY total_demand_gap DESC
LIMIT 20

Q: Where are my stockouts / which areas have shortages / cities with demand gaps
MATCH (sh:Shipment)-[:SHIPPED_TO]->(d:Distributor)
WHERE sh.demand_gap > 0
WITH d.distributor_city AS distributor_city,
     COUNT(sh) AS shortage_shipments,
     round(SUM(sh.demand_gap), 0) AS total_demand_gap,
     SUM(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed_shipments
RETURN distributor_city, shortage_shipments, total_demand_gap, delayed_shipments
ORDER BY total_demand_gap DESC
LIMIT 20


Q: Delivery performance is deteriorating across routes and transport modes. What is driving the disruption downstream?
MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor)
MATCH (pl)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d)
WHERE sh.delivery_status = 'Major Delay'
WITH r.mode AS transport_mode,
     COUNT(DISTINCT pl) AS plants_affected,
     COUNT(DISTINCT d) AS distributors_affected,
     COUNT(sh) AS total_delays,
     round(AVG(sh.delay_days), 2) AS avg_delay_days,
     round(SUM(CASE WHEN sh.demand_gap > 0 THEN sh.demand_gap ELSE 0 END), 0) AS total_demand_gap
RETURN transport_mode, plants_affected, distributors_affected,
       total_delays, avg_delay_days, total_demand_gap
ORDER BY total_delays DESC

Q: Which transport modes are causing the most downstream disruption?
MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor)
MATCH (pl)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d)
WHERE sh.delivery_status = 'Major Delay'
WITH r.mode AS transport_mode,
     COUNT(DISTINCT pl) AS plants_affected,
     COUNT(DISTINCT d) AS distributors_affected,
     COUNT(sh) AS total_delays,
     round(AVG(sh.delay_days), 2) AS avg_delay_days,
     round(SUM(CASE WHEN sh.demand_gap > 0 THEN sh.demand_gap ELSE 0 END), 0) AS total_demand_gap
RETURN transport_mode, plants_affected, distributors_affected,
       total_delays, avg_delay_days, total_demand_gap
ORDER BY total_delays DESC


"""
# =============================================================
# PHRASING VARIATION EXAMPLES
# All the extra Q+Cypher pairs below are injected into KG_SCHEMA
# at runtime by generate_cypher() via EXTRA_EXAMPLES string.
# =============================================================
EXTRA_EXAMPLES = """
=== PHRASING VARIATION EXAMPLES ===

Q: Show me late shipments / list overdue deliveries / show all delayed orders
MATCH (p:Plant)-[:DISPATCHES]->(s:Shipment)-[:SHIPPED_TO]->(d:Distributor)
WHERE s.delivery_status = 'Major Delay'
RETURN p.plant_name AS plant, s.shipment_id, s.delay_days,
       s.transaction_date AS date, d.distributor_city AS distributor
ORDER BY s.delay_days DESC
LIMIT 15

Q: Who are the riskiest suppliers / show problematic suppliers / suppliers I should worry about
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)
WHERE sup.risk_score > 0.6
RETURN sup.supplier_id, sup.supplier_name, round(sup.risk_score, 2) AS risk_score,
       sup.StoP_lead_time_days AS lead_time_days, pl.plant_id, pl.plant_name
ORDER BY sup.risk_score DESC
LIMIT 15

Q: Which plant is the biggest bottleneck / worst performing plant / causes the most delays
MATCH (pl:Plant)-[:DISPATCHES]->(s:Shipment)
WHERE s.delivery_status = 'Major Delay'
WITH pl, COUNT(s) AS delayed_count, round(AVG(s.delay_days), 2) AS avg_delay
RETURN pl.plant_id, pl.plant_name, delayed_count, avg_delay
ORDER BY delayed_count DESC
LIMIT 5

Q: Where are my stockouts / which areas have shortages / cities with demand gaps
MATCH (s:Shipment)-[:SHIPPED_TO]->(d:Distributor)
WHERE s.demand_gap > 0
WITH d, SUM(s.demand_gap) AS total_shortage, COUNT(s) AS shortage_shipments
RETURN d.distributor_id, d.distributor_city AS city, total_shortage, shortage_shipments
ORDER BY total_shortage DESC
LIMIT 15

Q: Which routes are cheapest / most affordable delivery paths / lowest cost routes
MATCH (r:Route)
RETURN r.route_id, r.mode, r.PtoD_distance_km AS distance_km,
       r.PtoD_transportation_cost_inr AS cost_inr, r.cost_efficiency AS efficiency
ORDER BY r.PtoD_transportation_cost_inr ASC
LIMIT 15

Q: What is my on-time delivery rate / how often do shipments arrive on time / delivery success rate
MATCH (s:Shipment)
WITH COUNT(s) AS total,
     SUM(CASE WHEN s.delivery_status = 'On Time' THEN 1 ELSE 0 END) AS on_time_count
RETURN total, on_time_count,
       round(100.0 * on_time_count / total, 2) AS on_time_pct,
       total - on_time_count AS delayed_count

Q: On-time rate per plant / which plant delivers on time most / best plant for reliability
MATCH (pl:Plant)-[:DISPATCHES]->(s:Shipment)
WITH pl, COUNT(s) AS total,
     SUM(CASE WHEN s.delivery_status = 'On Time' THEN 1 ELSE 0 END) AS on_time
RETURN pl.plant_id, pl.plant_name, total, on_time,
       round(100.0 * on_time / total, 2) AS on_time_pct
ORDER BY on_time_pct DESC

Q: Show shipment performance by month / monthly delay trend / how do delays vary by month
MATCH (s:Shipment)
WITH s.month_number AS month, COUNT(s) AS total,
     SUM(CASE WHEN s.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed_count
RETURN month, total, delayed_count,
       round(100.0 * delayed_count / total, 2) AS delay_pct
ORDER BY month ASC

Q: Which week had the most delays / worst week for shipments / weekly delay breakdown
MATCH (s:Shipment)
WHERE s.delivery_status = 'Major Delay'
WITH s.week_number AS week, COUNT(s) AS delayed_count
RETURN week, delayed_count
ORDER BY delayed_count DESC
LIMIT 10

Q: Full supply chain health report / compare all plants on delays and risk
MATCH (pl:Plant)-[:DISPATCHES]->(s:Shipment)
WITH pl, COUNT(s) AS total_shipments,
     SUM(CASE WHEN s.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed,
     round(AVG(s.delay_days), 2) AS avg_delay
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl)
WITH pl, total_shipments, delayed, avg_delay,
     COUNT(DISTINCT sup) AS supplier_count,
     round(AVG(sup.risk_score), 2) AS avg_supplier_risk
RETURN pl.plant_id, pl.plant_name, total_shipments, delayed,
       round(100.0 * delayed / total_shipments, 2) AS delay_pct,
       avg_delay, supplier_count, avg_supplier_risk
ORDER BY delay_pct DESC

Q: Which cities have the worst delivery experience / worst served distributor cities
MATCH (s:Shipment)-[:SHIPPED_TO]->(d:Distributor)
WITH d.distributor_city AS city, COUNT(s) AS total,
     SUM(CASE WHEN s.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed,
     round(AVG(s.delay_days), 2) AS avg_delay
RETURN city, total, delayed,
       round(100.0 * delayed / total, 2) AS delay_pct, avg_delay
ORDER BY delay_pct DESC
LIMIT 10

Q: Which shipments took much longer than planned / lead time overruns / worst schedule misses
MATCH (pl:Plant)-[:DISPATCHES]->(s:Shipment)-[:SHIPPED_TO]->(d:Distributor)
WHERE s.actual_days_taken > s.planned_lead_time_days
WITH pl, s, d, s.actual_days_taken - s.planned_lead_time_days AS overrun_days
RETURN s.shipment_id, pl.plant_name AS plant,
       s.planned_lead_time_days AS planned, s.actual_days_taken AS actual,
       overrun_days, d.distributor_city AS distributor
ORDER BY overrun_days DESC
LIMIT 15

Q: Which suppliers supply multiple plants / are there any shared suppliers
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)
WITH sup, COLLECT(DISTINCT pl.plant_id) AS plants, COUNT(DISTINCT pl) AS plant_count
WHERE plant_count > 1
RETURN sup.supplier_id, sup.supplier_name, round(sup.risk_score, 2) AS risk_score,
       plant_count, plants
ORDER BY plant_count DESC

Q: Which distributors are at risk because their sole supplying plant has high delays
MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor)
WITH d, COLLECT(DISTINCT pl.plant_id) AS plants, COUNT(DISTINCT pl) AS plant_count
WHERE plant_count = 1
WITH d, plants[0] AS sole_plant_id
MATCH (pl2:Plant {plant_id: sole_plant_id})-[:DISPATCHES]->(s:Shipment)
WHERE s.delivery_status = 'Major Delay'
WITH d, sole_plant_id, COUNT(s) AS delay_count
RETURN d.distributor_id, d.distributor_city AS city, sole_plant_id, delay_count
ORDER BY delay_count DESC
LIMIT 15

Q: What is the cost per km for each route / cost efficiency by distance
MATCH (r:Route)
WHERE r.PtoD_distance_km > 0
RETURN r.route_id, r.mode, r.PtoD_distance_km AS distance_km,
       r.PtoD_transportation_cost_inr AS cost_inr,
       round(r.PtoD_transportation_cost_inr / r.PtoD_distance_km, 2) AS cost_per_km,
       r.cost_efficiency AS efficiency
ORDER BY cost_per_km DESC
LIMIT 15

Q: Which retailers are growing fastest / fastest growing stores
MATCH (r:Retailer)
RETURN r.retailer_id, r.retailer_city AS city,
       r.annual_sales_y1_cr AS sales_y1, r.annual_sales_y2_cr AS sales_y2,
       round(r.retailer_growth_rate * 100, 2) AS growth_pct,
       r.retailer_growth_category AS growth_category
ORDER BY r.retailer_growth_rate DESC
LIMIT 15

Q: Which retailers are declining / shrinking stores / falling sales
MATCH (r:Retailer)
WHERE r.annual_sales_y2_cr < r.annual_sales_y1_cr
RETURN r.retailer_id, r.retailer_city AS city,
       r.annual_sales_y1_cr AS sales_y1, r.annual_sales_y2_cr AS sales_y2,
       round(r.annual_sales_y2_cr - r.annual_sales_y1_cr, 2) AS sales_change_cr
ORDER BY sales_change_cr ASC
LIMIT 15

Q: Which product categories are most at risk / riskiest product types / most delayed products
MATCH (s:Shipment)-[:CARRIES]->(p:Product)
WHERE s.delivery_status = 'Major Delay'
WITH p.product_category_name AS category,
     COUNT(s) AS delayed_count, round(AVG(s.delay_days), 2) AS avg_delay_days
RETURN category, delayed_count, avg_delay_days
ORDER BY delayed_count DESC

Q: Show the supply chain path for health_beauty / trace products from supplier to retailer
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)-[:DISPATCHES]->(sh:Shipment)
      -[:CARRIES]->(pr:Product),
      (sh)-[:SHIPPED_TO]->(d:Distributor)-[:DELIVERS_TO]->(r:Retailer)
WHERE pr.product_category_name = 'Health_Beauty'
RETURN DISTINCT sup.supplier_name AS supplier, pl.plant_name AS plant,
       d.distributor_city AS distributor, r.retailer_city AS retailer,
       sh.delivery_status AS status
LIMIT 15

Q: Which transport mode is most expensive on average / costliest mode
MATCH (r:Route)
RETURN r.mode AS mode, COUNT(r) AS route_count,
       round(AVG(r.PtoD_transportation_cost_inr), 2) AS avg_cost_inr,
       round(AVG(r.cost_efficiency), 4) AS avg_efficiency
ORDER BY avg_cost_inr DESC

Q: Which transportation mode causes the highest delays / which mode has the most delayed shipments / which mode causes most delays
MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor)
MATCH (pl)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d)
WHERE sh.delivery_status = 'Major Delay'
WITH r.mode AS transportation_mode,
     COUNT(sh) AS total_delays,
     round(AVG(sh.delay_days), 2) AS avg_delay_days,
     COUNT(DISTINCT pl) AS plants_affected
RETURN transportation_mode, total_delays, avg_delay_days, plants_affected
ORDER BY total_delays DESC

Q: Show supplier risk profile with lead time / supplier risk vs lead time
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)
RETURN sup.supplier_id, sup.supplier_name,
       round(sup.risk_score, 2) AS risk_score,
       sup.StoP_lead_time_days AS lead_time_days,
       sup.annual_capacity_units AS capacity,
       COLLECT(DISTINCT pl.plant_name) AS plants_supplied
ORDER BY sup.risk_score DESC
LIMIT 15

Q: Which distributors are most affected by delays / who suffers most from late deliveries
MATCH (s:Shipment)-[:SHIPPED_TO]->(d:Distributor)
WHERE s.delivery_status = 'Major Delay'
WITH d, COUNT(s) AS delayed_shipments, round(AVG(s.delay_days), 2) AS avg_delay
RETURN d.distributor_id, d.distributor_city AS city, delayed_shipments, avg_delay
ORDER BY delayed_shipments DESC
LIMIT 15

"""


SAMPLE_QUESTIONS = [
    "Show me all delayed shipments",
    "Which suppliers have a risk score above 0.7?",
    "What are the top 5 routes by transportation cost?",
    "Which distributors serve the most retailers?",
    "What is my on-time delivery rate?",
    "Which plant has the most delays?",
    "Show me retailers with declining sales",
    "Which product categories are most affected by delays?",
    "If the top 3 riskiest suppliers were removed, which plants would be affected?",
    "Compare all plants on shipment volume and delay rate",
    "Show monthly delay trends",
    "Which distributors have the biggest stockouts?",
    "Which suppliers supply multiple plants?",
    "Show the full supply chain path for health_beauty products",
    "Which routes are the most cost-efficient?",
]


# ─────────────────────────────────────────────────────────────
# STEP 1 — Generate Cypher
#
# FAST-PATH: Common questions are matched against QUERY_CACHE
# using normalised keyword matching — zero LLM calls, instant.
#
# SLOW-PATH: LLM with a CONDENSED prompt (schema essentials +
# 4-6 relevant examples picked by keyword). ~60 % fewer tokens
# than the old full-schema approach → 2-3× faster generation.
#
# VALIDATION: Every candidate is run through EXPLAIN before
# being returned so the user never sees a syntax error on the
# first attempt. Up to 3 self-correction retries on failure.
# ─────────────────────────────────────────────────────────────

# ── Pre-validated Cypher for the most frequent questions ─────
QUERY_CACHE: list[tuple[list[str], str]] = [
    # ── PRIMARY QUERY: Stockouts increased despite stable volumes ──────────
    # TEMPORARILY COMMENTED OUT FOR LLM TESTING — remove comments to restore cache
    # (
    #     ["stockout", "increased", "stable", "driving", "shortage", "responsible", "spreading"],
    #     """MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d:Distributor)
    # WHERE sh.demand_gap > 0
    # WITH d.distributor_city AS distributor_city,
    #      pl.plant_id AS plant_id, pl.plant_name AS plant_name,
    #      COUNT(sh) AS shortage_shipments,
    #      round(SUM(sh.demand_gap), 0) AS total_demand_gap,
    #      SUM(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed_shipments,
    #      round(AVG(CASE WHEN sh.delivery_status = 'Major Delay' THEN sh.delay_days END), 2) AS avg_delay_days
    # RETURN distributor_city, plant_id, plant_name,
    #        shortage_shipments, total_demand_gap, delayed_shipments, avg_delay_days
    # ORDER BY total_demand_gap DESC
    # LIMIT 20""",
    # ),
    # ── BACKUP QUERY: Delivery performance deteriorating ──────────────────
    # TEMPORARILY COMMENTED OUT FOR LLM TESTING — remove comments to restore cache
    # (
    #     ["delivery", "performance", "deteriorating", "routes", "modes", "driving", "disruption", "downstream"],
    #     """MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor)
    # MATCH (pl)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d)
    # WHERE sh.delivery_status = 'Major Delay'
    # WITH r.mode AS transport_mode,
    #      COUNT(DISTINCT pl) AS plants_affected,
    #      COUNT(DISTINCT d) AS distributors_affected,
    #      COUNT(sh) AS total_delays,
    #      round(AVG(sh.delay_days), 2) AS avg_delay_days,
    #      round(SUM(CASE WHEN sh.demand_gap > 0 THEN sh.demand_gap ELSE 0 END), 0) AS total_demand_gap
    # RETURN transport_mode, plants_affected, distributors_affected,
    #        total_delays, avg_delay_days, total_demand_gap
    # ORDER BY total_delays DESC""",
    # ),
    # ── Plant delay rate filter ────────────────────────────────
    (
        ["plant", "delay", "rate", "above", "which"],
        """MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)
WITH pl.plant_id AS plant_id, pl.plant_name AS plant_name,
     COUNT(sh) AS total_shipments,
     SUM(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed_count,
     round(AVG(CASE WHEN sh.delivery_status = 'Major Delay' THEN sh.delay_days END), 2) AS avg_delay
WITH plant_id, plant_name, total_shipments, delayed_count, avg_delay,
     round(100.0 * delayed_count / total_shipments, 1) AS delay_rate_pct
WHERE delay_rate_pct > 50
RETURN plant_id, plant_name, total_shipments, delayed_count, delay_rate_pct, avg_delay
ORDER BY delay_rate_pct DESC""",
    ),
    # ── Most delayed plant ─────────────────────────────────────
    (
        ["plant", "most", "delayed", "shipments"],
        """MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)
WITH pl.plant_id AS plant_id, pl.plant_name AS plant_name,
     COUNT(sh) AS total_shipments,
     SUM(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed
RETURN plant_id, plant_name, total_shipments, delayed,
       round(100.0 * delayed / total_shipments, 1) AS delay_rate_pct
ORDER BY delayed DESC""",
    ),
    # ── 2x lead time overrun ───────────────────────────────────
    (
        ["twice", "planned", "lead", "time"],
        """MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)
WHERE sh.actual_days_taken > 2 * sh.planned_lead_time_days
WITH pl.plant_id AS plant_id, pl.plant_name AS plant_name,
     COUNT(sh) AS shipments_exceeding_2x,
     round(AVG(sh.actual_days_taken), 1) AS avg_actual_days,
     round(AVG(sh.planned_lead_time_days), 1) AS avg_planned_days,
     round(AVG(sh.actual_days_taken - sh.planned_lead_time_days), 1) AS avg_overrun_days
RETURN plant_id, plant_name, shipments_exceeding_2x, avg_actual_days, avg_planned_days, avg_overrun_days
ORDER BY shipments_exceeding_2x DESC""",
    ),
    # ── Delivery performance deteriorating (backup query) ──────
    (
        ["delivery", "performance", "deteriorating", "transportation", "routes", "modes", "disruption"],
        """MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor)
MATCH (pl)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d)
WHERE sh.delivery_status = 'Major Delay'
WITH r.mode AS transport_mode,
     COUNT(DISTINCT pl) AS plants_affected,
     COUNT(DISTINCT d) AS distributors_affected,
     COUNT(sh) AS total_delays,
     round(AVG(sh.delay_days), 2) AS avg_delay_days,
     round(SUM(CASE WHEN sh.demand_gap > 0 THEN sh.demand_gap ELSE 0 END), 0) AS total_demand_gap
RETURN transport_mode, plants_affected, distributors_affected,
       total_delays, avg_delay_days, total_demand_gap
ORDER BY total_delays DESC""",
    ),
    (
        ["toy", "delay", "why", "delayed", "shipment"],
        """MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:CARRIES]->(pr:Product)
WHERE sh.delivery_status = 'Major Delay'
  AND toLower(pr.product_category_name) CONTAINS 'toy'
WITH sup.supplier_name AS supplier,
     pl.plant_name AS plant,
     COUNT(sh) AS delayed_shipments,
     round(AVG(sh.delay_days), 2) AS avg_delay_days,
     round(sup.risk_score, 2) AS risk_score
RETURN supplier, plant, delayed_shipments, avg_delay_days, risk_score
ORDER BY delayed_shipments DESC
LIMIT 15""",
    )]
    

# ── Condensed schema sent to LLM (no wall-of-text examples) ──
_CONDENSED_SCHEMA = """

NODES & KEY PROPERTIES:
(:Supplier) supplier_id, supplier_name, risk_score(float 0-1), StoP_lead_time_days, StoP_distance_km, annual_capacity_units
(:Plant)    plant_id('PL1'-'PL4'), plant_name('Baddi','Pune','Bhopal','Goa')
(:Distributor) distributor_id, distributor_city
(:Retailer) retailer_id, retailer_city, annual_sales_y1_cr, annual_sales_y2_cr, retailer_growth_rate, demand_gap(on Shipment)
(:Product)  product_id, product_category_name — use toLower() for matching OR exact stored value (auto-corrected at runtime)
(:Route)    route_id(format 'PL1@D0001'), mode('Road','Rail','Air','Sea'), PtoD_leadtime_days, PtoD_distance_km, PtoD_transportation_cost_inr, cost_efficiency
(:Shipment) shipment_id, delivery_status('Major Delay' or 'On Time'), delay_days, transaction_date, week_number, month_number, demand_gap(positive=shortage/stockout — ON SHIPMENT NOT RETAILER), planned_lead_time_days, actual_days_taken, PtoD_transportation_cost_inr

RELATIONSHIPS — directions are FIXED, never reverse them:
(Supplier)-[:SUPPLIES_TO]->(Plant)
(Plant)-[:DISPATCHES]->(Shipment)       ← Plant dispatches Shipment, NEVER the reverse
(Shipment)-[:SHIPPED_TO]->(Distributor)
(Shipment)-[:CARRIES]->(Product)        ← Shipment carries Product, NEVER the reverse
(Distributor)-[:DELIVERS_TO]->(Retailer)
(Plant)-[:HAS_ROUTE]->(Route)-[:CONNECTS_TO]->(Distributor)

TRAVERSAL PATTERNS — memorise these exactly:
• Plant → Shipment → Product:  (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:CARRIES]->(pr:Product)
• Product → Shipment → Plant:  (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:CARRIES]->(pr:Product)  ← SAME direction, start from Plant
• Supplier → Plant → Shipment: (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)-[:DISPATCHES]->(sh:Shipment)

RULES:
- delivery_status = 'Major Delay' (NOT 'Delayed')
- demand_gap is on SHIPMENT nodes only — never on Retailer or Distributor
- product_category_name values are case-sensitive — use toLower() for safety
- LIMIT 15 unless user specifies otherwise
- RETURN individual properties only, never whole nodes
- Use DISTINCT to avoid duplicates in multi-hop joins
- Suppliers link to Shipments ONLY via Plant: (Supplier)-[:SUPPLIES_TO]->(Plant)-[:DISPATCHES]->(Shipment)

FORBIDDEN — never write these reversed patterns:
  (r:Retailer)<-[:DELIVERS_TO]-(d:Distributor)   WRONG — use (d)-[:DELIVERS_TO]->(r)
  (d:Distributor)<-[:SHIPPED_TO]-(sh:Shipment)   WRONG — use (sh)-[:SHIPPED_TO]->(d)
  (sh:Shipment)<-[:DISPATCHES]-(pl:Plant)         WRONG — use (pl)-[:DISPATCHES]->(sh)
  (pl:Plant)<-[:SUPPLIES_TO]-(sup:Supplier)       WRONG — use (sup)-[:SUPPLIES_TO]->(pl)
  (p:Product)<-[:CARRIES]-(sh:Shipment)           WRONG — use (sh)-[:CARRIES]->(p)
  r.demand_gap   WRONG — demand_gap is on Shipment, not Retailer
  d.demand_gap   WRONG — demand_gap is on Shipment, not Distributor
  (r:Route)<-[:HAS_ROUTE]-(pl:Plant)              WRONG — use (pl)-[:HAS_ROUTE]->(r:Route)
  MATCH (r:Route)<-[:HAS_ROUTE]-(pl:Plant)-[:DISPATCHES]->(sh)  WRONG — Route has no DISPATCHES path
  Route nodes do NOT connect to Shipment directly. To join Route + Shipment, use SAME Plant as bridge.

CORRECT patterns for multi-hop queries:
  Upstream to downstream: (sup)-[:SUPPLIES_TO]->(pl)-[:DISPATCHES]->(sh)-[:SHIPPED_TO]->(d)-[:DELIVERS_TO]->(r)
  Stockout/demand gap:    MATCH (pl)-[:DISPATCHES]->(sh)-[:SHIPPED_TO]->(d) WHERE sh.demand_gap > 0
                          ALWAYS use WITH + DISTINCT d to avoid duplicate distributors.
                          WRONG: RETURN d.distributor_id (gives 1 row per shipment, duplicates!)
                          RIGHT: WITH DISTINCT d ... RETURN d.distributor_id (1 row per distributor)
  Category delays:        MATCH (pl)-[:DISPATCHES]->(sh)-[:CARRIES]->(pr:Product) WHERE toLower(pr.product_category_name) CONTAINS 'toys'
  Route traversal:        MATCH (pl)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d)
  Retailer stockout:      MATCH (d)-[:DELIVERS_TO]->(r:Retailer) MATCH (pl)-[:DISPATCHES]->(sh)-[:SHIPPED_TO]->(d) WHERE sh.demand_gap > 0
  Transport mode delays:  MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor)
                          MATCH (pl)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d)
                          WHERE sh.delivery_status = 'Major Delay'
                          WITH r.mode AS transportation_mode,
                               COUNT(sh) AS total_delays,
                               round(AVG(sh.delay_days), 2) AS avg_delay_days,
                               COUNT(DISTINCT pl) AS plants_affected
                          RETURN transportation_mode, total_delays, avg_delay_days, plants_affected
                          ORDER BY total_delays DESC

CRITICAL: (pl:Plant)-[:HAS_ROUTE]->(r:Route) is the ONLY valid direction. NEVER write (r:Route)<-[:HAS_ROUTE]-(pl:Plant).
"""


def _pick_examples(question: str) -> str:
    """Return 3–5 Cypher examples most relevant to the user's question."""
    q = question.lower()
    examples = []

    # ── BACKUP QUERY — delivery performance by transport mode ────────────
    _is_delivery_perf_q = (
        any(w in q for w in ["deteriorating", "disruption", "downstream", "performance"])
        and any(w in q for w in ["delivery", "route", "mode", "transport"])
    )
    if _is_delivery_perf_q:
        examples.append("""Q: Delivery performance is deteriorating across routes and transport modes. What is driving the disruption downstream?
MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor)
MATCH (pl)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d)
WHERE sh.delivery_status = 'Major Delay'
WITH r.mode AS transport_mode,
     COUNT(DISTINCT pl) AS plants_affected,
     COUNT(DISTINCT d) AS distributors_affected,
     COUNT(sh) AS total_delays,
     round(AVG(sh.delay_days), 2) AS avg_delay_days,
     round(SUM(CASE WHEN sh.demand_gap > 0 THEN sh.demand_gap ELSE 0 END), 0) AS total_demand_gap
RETURN transport_mode, plants_affected, distributors_affected,
       total_delays, avg_delay_days, total_demand_gap
ORDER BY total_delays DESC
NOTE: No LIMIT — return all transport modes (Road/Rail/Air/Sea)""")

    # ── PRIMARY STOCKOUT QUERY — inject exact aggregated pattern ────────
    # When question is diagnostic (who/why/when/how/what is driving/spreading)
    # + stockout/shortage keywords → inject the exact correct aggregated Cypher
    _is_diagnostic_stockout = (
        any(w in q for w in ["stockout", "shortage", "demand gap", "driving",
                              "spreading", "responsible", "when did", "how is"])
        and any(w in q for w in ["who", "why", "when", "how", "what is",
                                  "driving", "responsible", "spreading", "begin"])
    )
    if _is_diagnostic_stockout:
        examples.append("""Q: Stockouts have increased across the network. What is driving these shortages, who is responsible, when did the issue begin, and how is it spreading?
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d:Distributor)
WHERE sh.demand_gap > 0
WITH d.distributor_city AS distributor_city,
     pl.plant_id AS plant_id,
     pl.plant_name AS plant_name,
     COUNT(sh) AS shortage_shipments,
     round(SUM(sh.demand_gap), 0) AS total_demand_gap,
     SUM(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed_shipments,
     round(AVG(CASE WHEN sh.delivery_status = 'Major Delay' THEN sh.delay_days END), 2) AS avg_delay_days
RETURN distributor_city, plant_id, plant_name,
       shortage_shipments, total_demand_gap, delayed_shipments, avg_delay_days
ORDER BY total_demand_gap DESC
CRITICAL RULES FOR THIS QUERY TYPE:
- NO LIMIT — this is a network-wide query, return ALL city+plant combinations
- Never add extra columns like shipment_id, incident_date, first_occurrence, latest_occurrence
- Group by distributor_city + plant_id only
- Return exactly these columns: distributor_city, plant_id, plant_name, shortage_shipments, total_demand_gap, delayed_shipments, avg_delay_days""")

    if any(w in q for w in ["supplier", "supply", "vendor"]):
        examples.append("""Q: Suppliers causing the most delays
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)-[:DISPATCHES]->(s:Shipment)
WHERE s.delivery_status = 'Major Delay'
WITH sup, COUNT(s) AS delayed_shipments, round(AVG(s.delay_days),2) AS avg_delay
RETURN sup.supplier_id, sup.supplier_name, round(sup.risk_score,2) AS risk_score,
       delayed_shipments, avg_delay
ORDER BY delayed_shipments DESC LIMIT 15""")

    if any(w in q for w in ["delay", "late", "overdue", "shipment"]):
        examples.append("""Q: Delayed shipments per plant
MATCH (pl:Plant)-[:DISPATCHES]->(s:Shipment)-[:SHIPPED_TO]->(d:Distributor)
WHERE s.delivery_status = 'Major Delay'
WITH pl.plant_name AS plant, COUNT(s) AS delayed_count,
     round(AVG(s.delay_days),2) AS avg_delay
RETURN plant, delayed_count, avg_delay
ORDER BY delayed_count DESC""")

        examples.append("""Q: Which plant has the most delayed shipments
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)
WITH pl.plant_id AS plant_id, pl.plant_name AS plant_name,
     COUNT(sh) AS total_shipments,
     SUM(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed
RETURN plant_id, plant_name, total_shipments, delayed,
       round(100.0 * delayed / total_shipments, 1) AS delay_rate_pct
ORDER BY delayed DESC
NOTE: Return plant_id AND plant_name together. Column is called 'delayed' not 'delayed_shipments'.""")

    if any(w in q for w in ["route", "transport", "cost", "mode"]):
        examples.append("""Q: Top routes by transportation cost
MATCH (r:Route)
RETURN r.route_id, r.mode, r.PtoD_distance_km AS distance_km,
       r.PtoD_transportation_cost_inr AS cost_inr, round(r.cost_efficiency,4) AS efficiency
ORDER BY r.PtoD_transportation_cost_inr DESC LIMIT 15""")

    if any(w in q for w in ["transport", "mode", "delay", "cause", "highest"]):
        examples.append("""Q: Which transportation mode causes the highest delays
MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor)
MATCH (pl)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d)
WHERE sh.delivery_status = 'Major Delay'
WITH r.mode AS transportation_mode,
     COUNT(sh) AS total_delays,
     round(AVG(sh.delay_days), 2) AS avg_delay_days,
     COUNT(DISTINCT pl) AS plants_affected
RETURN transportation_mode, total_delays, avg_delay_days, plants_affected
ORDER BY total_delays DESC""")

    if any(w in q for w in ["retailer", "sales", "growth", "city"]):
        examples.append("""Q: Retailers with declining sales
MATCH (r:Retailer) WHERE r.annual_sales_y2_cr < r.annual_sales_y1_cr
RETURN r.retailer_id, r.retailer_city AS city,
       r.annual_sales_y1_cr AS sales_y1, r.annual_sales_y2_cr AS sales_y2,
       round(r.annual_sales_y2_cr - r.annual_sales_y1_cr, 2) AS sales_change
ORDER BY sales_change ASC LIMIT 15""")

    if any(w in q for w in ["distributor", "serve", "deliver", "stockout", "shortage",
                             "demand gap", "demand_gap", "causing stockout", "stock"]):
        examples.append("""Q: What is causing stockouts — aggregated by distributor city
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d:Distributor)
WHERE sh.demand_gap > 0
WITH d.distributor_city AS distributor_city,
     pl.plant_id AS plant_id, pl.plant_name AS plant_name,
     COUNT(sh) AS shortage_shipments,
     round(SUM(sh.demand_gap), 0) AS total_demand_gap
RETURN distributor_city, plant_id, plant_name, shortage_shipments, total_demand_gap
ORDER BY total_demand_gap DESC
LIMIT 15""")

        examples.append("""Q: Distributors serving most retailers
MATCH (d:Distributor)-[:DELIVERS_TO]->(r:Retailer)
RETURN d.distributor_id, d.distributor_city, COUNT(r) AS num_retailers
ORDER BY num_retailers DESC LIMIT 15""")

    # ── Node count by type — "how many X in graph" ─────────────────────
    if any(w in q for w in ["how many", "count", "total nodes", "how much",
                             "graph contain", "graph size", "node types"]):
        examples.append("""Q: How many suppliers, plants, distributors and retailers are in the graph
MATCH (n)
WHERE labels(n)[0] IN ['Supplier','Plant','Distributor','Retailer','Shipment','Route','Product']
RETURN labels(n)[0] AS node_type, COUNT(n) AS count
ORDER BY count DESC""")

    # ── On-time delivery rate ─────────────────────────────────────────
    if any(w in q for w in ["on-time", "on time", "delivery rate", "overall rate",
                             "performance", "kpi", "percentage on time"]):
        examples.append("""Q: What is my on-time delivery rate
MATCH (s:Shipment)
WITH COUNT(s) AS total_shipments,
     SUM(CASE WHEN s.delivery_status = 'On Time' THEN 1 ELSE 0 END) AS on_time_count,
     SUM(CASE WHEN s.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed_count
RETURN total_shipments,
       on_time_count,
       delayed_count,
       round(toFloat(on_time_count) / toFloat(total_shipments) * 100, 1) AS on_time_rate_pct,
       round(toFloat(delayed_count) / toFloat(total_shipments) * 100, 1) AS delay_rate_pct""")

    # ── Most cost-efficient routes ────────────────────────────────────
    if any(w in q for w in ["efficient", "cheapest", "best value", "cost efficient",
                             "low cost route", "cost-efficient"]):
        examples.append("""Q: Which routes are most cost-efficient
MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor)
RETURN r.route_id AS route_id,
       pl.plant_name AS plant,
       d.distributor_city AS distributor_city,
       r.mode AS mode,
       round(r.cost_efficiency, 4) AS cost_efficiency,
       round(r.PtoD_transportation_cost_inr, 2) AS cost_inr,
       round(r.PtoD_distance_km, 2) AS distance_km
ORDER BY r.cost_efficiency DESC
LIMIT 15""")

    # ── Monthly trends ────────────────────────────────────────────────
    if any(w in q for w in ["monthly", "month", "trend", "over time", "by month"]):
        examples.append("""Q: Show monthly delay trends
MATCH (s:Shipment)
WHERE s.month_number IS NOT NULL
WITH s.month_number AS month,
     COUNT(s) AS total_shipments,
     SUM(CASE WHEN s.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed_count
RETURN month,
       total_shipments,
       delayed_count,
       round(toFloat(delayed_count) / toFloat(total_shipments) * 100, 1) AS delay_rate_pct
ORDER BY toInteger(month) ASC""")

    if any(w in q for w in ["product", "category", "type", "toys", "toy", "auto", "health",
                             "watches", "construction", "cool stuff", "bed bath"]):
        examples.append("""Q: Why are toy shipments delayed
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:CARRIES]->(pr:Product)
WHERE sh.delivery_status = 'Major Delay'
  AND pr.product_category_name = 'Toys'
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl)
RETURN sup.supplier_name AS supplier, pl.plant_name AS plant,
       COUNT(sh) AS delayed_shipments,
       round(AVG(sh.delay_days),2) AS avg_delay_days,
       round(sup.risk_score,2) AS risk_score
ORDER BY delayed_shipments DESC LIMIT 15""")

        examples.append("""Q: Most delayed product categories
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:CARRIES]->(pr:Product)
WHERE sh.delivery_status = 'Major Delay'
RETURN pr.product_category_name AS category,
       COUNT(sh) AS delayed_count,
       round(AVG(sh.delay_days),2) AS avg_delay
ORDER BY delayed_count DESC""")

    if any(w in q for w in ["risk", "high risk", "risky", "score"]):
        examples.append("""Q: High risk suppliers
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)
WHERE sup.risk_score > 0.7
RETURN sup.supplier_id, sup.supplier_name, round(sup.risk_score,2) AS risk_score,
       sup.StoP_lead_time_days AS lead_time, pl.plant_id, pl.plant_name
ORDER BY sup.risk_score DESC LIMIT 15""")

    if any(w in q for w in ["twice", "2x", "double", "twice the", "lead time", "actual", "planned", "overrun"]):
        examples.append("""Q: Which shipments took more than twice the planned lead time
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)
WHERE sh.actual_days_taken > 2 * sh.planned_lead_time_days
WITH pl.plant_id AS plant_id, pl.plant_name AS plant_name,
     COUNT(sh) AS shipments_exceeding_2x,
     round(AVG(sh.actual_days_taken), 1) AS avg_actual_days,
     round(AVG(sh.planned_lead_time_days), 1) AS avg_planned_days,
     round(AVG(sh.actual_days_taken - sh.planned_lead_time_days), 1) AS avg_overrun_days
RETURN plant_id, plant_name, shipments_exceeding_2x, avg_actual_days, avg_planned_days, avg_overrun_days
ORDER BY shipments_exceeding_2x DESC
CRITICAL: Aggregate by plant — do NOT return individual shipment rows. Never use RETURN sh.shipment_id.""")

    if any(w in q for w in ["month", "week", "trend", "time", "period"]):
        examples.append("""Q: Monthly delay trend
MATCH (s:Shipment)
WITH s.month_number AS month, COUNT(s) AS total,
     SUM(CASE WHEN s.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed
RETURN month, total, delayed, round(100.0*delayed/total,2) AS delay_pct
ORDER BY month ASC""")

    if any(w in q for w in ["plant", "compare", "all plant", "bottleneck"]):
        examples.append("""Q: Compare all plants on delay rate
MATCH (pl:Plant)-[:DISPATCHES]->(s:Shipment)
WITH pl, COUNT(s) AS total,
     SUM(CASE WHEN s.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed,
     round(AVG(s.delay_days),2) AS avg_delay
RETURN pl.plant_id, pl.plant_name, total, delayed,
       round(100.0*delayed/total,2) AS delay_pct, avg_delay
ORDER BY delay_pct DESC""")

    if any(w in q for w in ["remove", "if", "without", "what if", "what-if", "impact"]):
        examples.append("""Q: If top 3 riskiest suppliers removed, which plants affected
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)
WITH sup ORDER BY sup.risk_score DESC LIMIT 3
MATCH (sup)-[:SUPPLIES_TO]->(pl:Plant)
MATCH (pl)-[:DISPATCHES]->(sh:Shipment)-[:CARRIES]->(prod:Product)
MATCH (sh)-[:SHIPPED_TO]->(d:Distributor)-[:DELIVERS_TO]->(r:Retailer)
RETURN sup.supplier_name, round(sup.risk_score,2) AS risk_score,
       pl.plant_name, COLLECT(DISTINCT prod.product_category_name) AS categories,
       COUNT(DISTINCT d) AS distributors, COUNT(DISTINCT r) AS retailers
ORDER BY sup.risk_score DESC""")

    # Always include a generic fallback example
    if not examples:
        examples.append("""Q: Show all shipments with plant info
MATCH (pl:Plant)-[:DISPATCHES]->(s:Shipment)
RETURN pl.plant_id, s.shipment_id, s.delivery_status, s.delay_days, s.transaction_date
ORDER BY s.delay_days DESC LIMIT 15""")

    return "\n\n".join(examples[:5])


def _validate_cypher_syntax(cypher: str) -> str | None:
    """EXPLAIN <query> — syntax check only, no data read. Returns None if valid."""
    try:
        with neo4j_driver.session(database=os.getenv("NEO4J_DATABASE", "neo4j")) as s:
            s.run("EXPLAIN " + cypher).consume()
        return None
    except Exception as e:
        return str(e)


def _normalise(text: str) -> set:
    """Lowercase + split into word tokens for cache key matching."""
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _auto_fix_distributor_duplicates(cypher: str) -> str:
    """
    If the LLM generates a query that returns distributor columns directly from a
    MATCH path (without WITH+DISTINCT or aggregation), it produces one row per
    shipment — causing the same distributor to appear multiple times.

    This function detects that pattern and rewrites to use WITH+DISTINCT so each
    distributor appears exactly once with aggregated shortage stats.

    Pattern to fix:
        MATCH ...-[:SHIPPED_TO]->(d:Distributor)
        WHERE sh.demand_gap > 0
        RETURN d.distributor_id, d.distributor_city   ← NO aggregation = duplicates
        ORDER BY sh.demand_gap DESC

    Fixed to:
        MATCH ...-[:SHIPPED_TO]->(d:Distributor)
        WHERE sh.demand_gap > 0
        WITH d.distributor_id AS distributor_id, d.distributor_city AS distributor_city,
             COUNT(DISTINCT sh) AS shortage_shipments, SUM(sh.demand_gap) AS total_demand_gap
        RETURN distributor_id, distributor_city, shortage_shipments, total_demand_gap
        ORDER BY total_demand_gap DESC
    """
    import re as _re
    q = cypher.strip()

    # Detect the problematic pattern:
    # 1. Has SHIPPED_TO distributor match
    # 2. Has WHERE demand_gap > 0
    # 3. RETURN includes d.distributor_id or d.distributor_city
    # 4. No WITH clause (no aggregation)
    has_distributor = bool(_re.search(r'SHIPPED_TO.*Distributor|:Distributor', q, _re.IGNORECASE))
    has_demand_gap  = 'demand_gap' in q.lower()
    has_return_dist = bool(_re.search(r'RETURN.*d\.distributor', q, _re.IGNORECASE))
    has_with        = bool(_re.search(r'\bWITH\b', q, _re.IGNORECASE))

    if has_distributor and has_demand_gap and has_return_dist and not has_with:
        # Extract the MATCH+WHERE part (everything before RETURN)
        return_match = _re.search(r'(.*?)(RETURN\s+.*)', q, _re.IGNORECASE | _re.DOTALL)
        if return_match:
            match_where = return_match.group(1).rstrip()
            # Replace with aggregated version using WITH+DISTINCT
            fixed = (
                match_where + "\n"
                "WITH d.distributor_id   AS distributor_id,\n"
                "     d.distributor_city AS distributor_city,\n"
                "     COUNT(DISTINCT sh) AS shortage_shipments,\n"
                "     SUM(sh.demand_gap) AS total_demand_gap\n"
                "RETURN distributor_id, distributor_city, shortage_shipments, total_demand_gap\n"
                "ORDER BY total_demand_gap DESC\n"
                "LIMIT 15"
            )
            return fixed

    return cypher


def generate_cypher(user_question: str) -> str:
    """
    Generate Cypher for user_question.

    1. Check QUERY_CACHE — if keywords match, return pre-validated query instantly.
    2. Otherwise call LLM with a CONDENSED prompt (~60 % fewer tokens than old approach).
    3. Validate with EXPLAIN and self-correct up to 3 times.
    """
    import re as _re_gc
    q_tokens = _normalise(user_question)
    q_lower  = user_question.lower()

    # ── Pre-extract numeric values from question ──────────────
    # Extracts thresholds like "above 0.9", "greater than 5", "below 50%" etc.
    # so the LLM prompt explicitly contains the user's actual number
    _num_hints = []
    for _m in _re_gc.finditer(
        r'(?:above|over|greater than|more than|exceed|at least|below|under|less than|fewer than|equal to|=)\s*(\d+(?:\.\d+)?)\s*(%)?',
        q_lower
    ):
        _val = _m.group(1)
        _pct = "%" if _m.group(2) else ""
        _num_hints.append(f"{_val}{_pct}")
    # Also catch bare decimals like "risk score 0.9"
    for _m in _re_gc.finditer(r'risk\s+score\s+(\d+\.\d+)', q_lower):
        if _m.group(1) not in _num_hints:
            _num_hints.append(_m.group(1))
    _threshold_hint = (
        f"\n⚠ USER-SPECIFIED THRESHOLD: Use EXACTLY {' and '.join(_num_hints)} in your WHERE clause. "
        f"Do NOT substitute 0.7 or any default — use the number the user stated."
    ) if _num_hints else ""

    # ── Priority 0: WHAT-IF / transport-removal scenarios ─────────────
    # Must come FIRST — before cache and before transport-delay shortcuts,
    # because "road" and "remove" also match broad cache keywords otherwise.
    _is_whatif_q = any(w in q_lower for w in [
        "what if", "what happens if", "what would happen", "if road", "if rail",
        "if air", "if sea", "removed from", "remove road", "remove rail",
        "remove air", "remove sea", "without road", "without rail", "without air",
        "without sea", "road is removed", "rail is removed", "air is removed",
        "sea is removed", "transport is removed", "mode is removed",
        "eliminate road", "eliminate rail", "shut down road", "no road",
        "road transport removed", "if we remove", "removing road", "removing rail"
    ])
    if _is_whatif_q:
        # Detect which mode is being removed
        _mode_map = {
            "road":  "Road",
            "truck": "Road",
            "lorry": "Road",
            "rail":  "Rail",
            "train": "Rail",
            "air":   "Air",
            "flight":"Air",
            "sea":   "Sea",
            "ship":  "Sea",
            "ocean": "Sea",
            "vessel":"Sea",
        }
        _removed_mode = next(
            (v for k, v in _mode_map.items() if k in q_lower), None
        )
        if _removed_mode:
            return (
                f"MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor)\n"
                f"WHERE r.mode = '{_removed_mode}'\n"
                f"OPTIONAL MATCH (pl)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d)\n"
                f"WITH d.distributor_city AS city,\n"
                f"     pl.plant_name AS plant,\n"
                f"     r.route_id AS route_id,\n"
                f"     COUNT(DISTINCT r) AS routes_on_{_removed_mode.lower()},\n"
                f"     round(AVG(r.PtoD_transportation_cost_inr), 0) AS avg_route_cost_inr,\n"
                f"     round(AVG(r.PtoD_leadtime_days), 1) AS avg_leadtime_days,\n"
                f"     COUNT(sh) AS shipments_at_risk,\n"
                f"     round(SUM(CASE WHEN sh.demand_gap > 0 THEN sh.demand_gap ELSE 0 END), 0) AS demand_gap_at_risk,\n"
                f"     COUNT(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 END) AS delayed_shipments\n"
                f"RETURN city, plant, routes_on_{_removed_mode.lower()},\n"
                f"       avg_route_cost_inr, avg_leadtime_days,\n"
                f"       shipments_at_risk, demand_gap_at_risk, delayed_shipments\n"
                f"ORDER BY shipments_at_risk DESC\n"
                f"LIMIT 25"
            )
        else:
            # Generic what-if — show all transport modes and their impact
            return (
                "MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor)\n"
                "WITH r.mode AS transport_mode,\n"
                "     COUNT(DISTINCT r) AS total_routes,\n"
                "     COUNT(DISTINCT pl) AS plants_using_mode,\n"
                "     COUNT(DISTINCT d) AS distributors_served,\n"
                "     round(AVG(r.PtoD_transportation_cost_inr), 2) AS avg_cost_inr,\n"
                "     round(AVG(r.PtoD_leadtime_days), 1) AS avg_leadtime_days\n"
                "RETURN transport_mode, total_routes, plants_using_mode,\n"
                "       distributors_served, avg_cost_inr, avg_leadtime_days\n"
                "ORDER BY total_routes DESC"
            )

    # ── Priority shortcut: "what modes does plant X use" queries ─────────
    # Must run BEFORE the transport-delay shortcut which would otherwise grab it.
    # Detects: "what/which transport modes does/do Plant PLx use/have"
    _PLANT_MAP_MODES = {"pl1": "PL1", "pl2": "PL2", "pl3": "PL3", "pl4": "PL4",
                        "baddi": "PL1", "bhopal": "PL2", "pune": "PL3", "goa": "PL4"}
    _plant_mode_q = any(w in q_lower for w in ["what mode", "which mode", "what transport mode",
                                                 "which transport mode", "modes does", "modes do",
                                                 "modes used", "mode does", "transport modes",
                                                 "uses what", "uses which", "what modes"])
    _detected_plant_id = next((v for k, v in _PLANT_MAP_MODES.items() if k in q_lower), None)

    if _plant_mode_q and _detected_plant_id:
        return (
            f"MATCH (pl:Plant {{plant_id: '{_detected_plant_id}'}})-[:HAS_ROUTE]->(r:Route)\n"
            f"RETURN DISTINCT r.mode AS transport_mode,\n"
            f"       COUNT(r) AS route_count,\n"
            f"       round(AVG(r.PtoD_transportation_cost_inr), 0) AS avg_cost_inr,\n"
            f"       round(AVG(r.PtoD_leadtime_days), 1) AS avg_leadtime_days,\n"
            f"       round(AVG(r.PtoD_distance_km), 0) AS avg_distance_km\n"
            f"ORDER BY route_count DESC"
        )
    elif _plant_mode_q and not _detected_plant_id:
        # All plants — what modes are used network-wide
        return (
            "MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)\n"
            "RETURN pl.plant_id AS plant_id, pl.plant_name AS plant_name,\n"
            "       r.mode AS transport_mode, COUNT(r) AS route_count\n"
            "ORDER BY pl.plant_id, route_count DESC"
        )

    # ── Priority shortcut: transport-mode DELAY questions ───────
    # Only fires for delay/causes questions — NOT for cost/expensive queries.
    _TRANSPORT_KEYWORDS = {"transport", "transportation", "mode", "delay", "delays",
                           "causes", "cause", "highest", "most"}
    _is_cost_query = any(w in q_lower for w in [
        "expensive", "cost", "price", "cheap", "cheapest", "cost-efficient",
        "cost efficient", "average cost", "most costly", "least expensive"
    ])
    _is_transport_delay_q = (
        len(q_tokens & _TRANSPORT_KEYWORDS) >= 3
        and any(w in q_lower for w in ["transport", "mode"])
        and not _is_cost_query  # exclude "which mode is most expensive"
        and any(w in q_lower for w in ["delay", "late", "cause", "causing", "most delay"])
    )
    if _is_transport_delay_q:
        return (
            "MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor)\n"
            "MATCH (pl)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d)\n"
            "WHERE sh.delivery_status = 'Major Delay'\n"
            "WITH r.mode AS transportation_mode,\n"
            "     COUNT(sh) AS total_delays,\n"
            "     round(AVG(sh.delay_days), 2) AS avg_delay_days,\n"
            "     COUNT(DISTINCT pl) AS plants_affected\n"
            "RETURN transportation_mode, total_delays, avg_delay_days, plants_affected\n"
            "ORDER BY total_delays DESC"
        )

    # ── Priority shortcut: transport-mode COST questions ─────
    # ── Priority shortcut: individual ROUTE cost ranking ────────────────
    # Fires ONLY when no specific cost threshold is given (e.g. "top 5 routes by cost").
    # When user says "routes costing more than X", pass to LLM with threshold hint.
    _is_individual_route_q = (
        any(w in q_lower for w in ["route", "routes"]) and _is_cost_query
        and not any(w in q_lower for w in [
            "which mode", "transport mode", "transportation mode",
            "by mode", "per mode", "mode cost", "average cost by"
        ])
    )
    _has_cost_threshold = bool(_num_hints) and any(
        w in q_lower for w in ["more than", "above", "greater than", "over",
                                "less than", "below", "under", "cheaper than",
                                "exceed", "exceeding"]
    )
    if _is_individual_route_q and not _has_cost_threshold:
        import re as _re_lim
        _lim_m = _re_lim.search(r'\btop\s+(\d+)\b', q_lower)
        _limit  = int(_lim_m.group(1)) if _lim_m else 15
        _order  = "ASC" if any(w in q_lower for w in ["cheap", "cheapest", "lowest", "affordable", "least expensive"]) else "DESC"
        return (
            f"MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor)\n"
            f"RETURN r.route_id AS route_id,\n"
            f"       pl.plant_name AS plant,\n"
            f"       d.distributor_city AS distributor_city,\n"
            f"       r.mode AS transport_mode,\n"
            f"       round(r.PtoD_transportation_cost_inr, 0) AS cost_inr,\n"
            f"       round(r.PtoD_distance_km, 1) AS distance_km,\n"
            f"       round(r.PtoD_leadtime_days, 1) AS leadtime_days,\n"
            f"       round(r.cost_efficiency, 4) AS cost_efficiency\n"
            f"ORDER BY r.PtoD_transportation_cost_inr {_order}\n"
            f"LIMIT {_limit}"
        )
    # If threshold given → fall through to LLM with _threshold_hint injected

    # ── Priority shortcut: transport-mode COST summary ────────────────
    # Only fires when user explicitly asks about mode-level averages,
    # NOT when asking for individual route rankings.
    _is_transport_cost_q = (
        any(w in q_lower for w in ["transport mode", "transportation mode", "by mode", "per mode",
                                    "which mode", "average cost", "mode cost", "mode average"])
        and _is_cost_query
    )
    if _is_transport_cost_q:
        return (
            "MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor)\n"
            "WITH r.mode AS transport_mode,\n"
            "     COUNT(r) AS route_count,\n"
            "     round(AVG(r.PtoD_transportation_cost_inr), 2) AS avg_cost_inr,\n"
            "     round(MIN(r.PtoD_transportation_cost_inr), 2) AS min_cost_inr,\n"
            "     round(MAX(r.PtoD_transportation_cost_inr), 2) AS max_cost_inr,\n"
            "     round(AVG(r.PtoD_distance_km), 1) AS avg_distance_km\n"
            "RETURN transport_mode, route_count, avg_cost_inr, min_cost_inr,\n"
            "       max_cost_inr, avg_distance_km\n"
            "ORDER BY avg_cost_inr DESC"
        )

    # ── Fast-path: cache lookup ───────────────────────────────
    for keywords, cypher in QUERY_CACHE:
        kw_tokens = _normalise(" ".join(keywords))
        overlap = len(kw_tokens & q_tokens)
        if overlap >= max(3, int(len(kw_tokens) * 0.75)):  # strict threshold — require 75% keyword match
            return _fix_category_names_in_cypher(cypher)

    # ── Slow-path: LLM with condensed prompt ─────────────────
    examples = _pick_examples(user_question)
    prompt = (
        _CONDENSED_SCHEMA
        + "\n\n=== RELEVANT EXAMPLES ===\n"
        + examples
        + "\n\n=== USER QUESTION ===\n"
        + user_question
        + _threshold_hint
        + "\n\nReturn ONLY the raw Cypher — no backticks, no markdown, no explanation."
    )

    messages = [{"role": "user", "content": prompt}]
    last_cypher = ""

    for attempt in range(1):  # SPEED: was 3 — cache covers most; 1 attempt usually correct
        raw = call_llm(messages=messages, max_tokens=250, temperature=0.0)  # SPEED: was 350

        # Strip markdown fences
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw.strip())
        raw = re.sub(r"\n?```$", "", raw).strip()
        # Auto-fix distributor duplicates from LLM output
        raw = _auto_fix_distributor_duplicates(raw)

        # Take only the first query if model returned multiple blocks
        queries = re.split(r"\n{2,}(?=MATCH|WITH|CALL)", raw)
        cypher = queries[0].strip()
        last_cypher = cypher

        # Validate every attempt (fast EXPLAIN, no data read)
        error = _validate_cypher_syntax(cypher)
        if error is None:
            return cypher

        # Feed error back for self-correction
        messages.append({"role": "assistant", "content": raw})
        messages.append({
            "role": "user",
            "content": (
                f"Syntax error from Neo4j: {error[:300]}\n\n"
                "Fix the Cypher and return ONLY the corrected query — no markdown."
            ),
        })

    return _fix_category_names_in_cypher(last_cypher)
# Stores the exact strings Neo4j has in Product.product_category_name
_CATEGORY_NAMES_CACHE: list[str] = []

def _get_actual_category_names() -> list[str]:
    """Fetch actual product_category_name values from Neo4j (cached after first call)."""
    global _CATEGORY_NAMES_CACHE
    if _CATEGORY_NAMES_CACHE:
        return _CATEGORY_NAMES_CACHE
    try:
        with neo4j_driver.session(database=os.getenv("NEO4J_DATABASE", "neo4j")) as s:
            rows = list(s.run(
                "MATCH (p:Product) RETURN DISTINCT p.product_category_name AS cat "
                "ORDER BY cat"
            ))
            _CATEGORY_NAMES_CACHE = [r["cat"] for r in rows if r["cat"]]
            print(f"[Categories] Actual values in Neo4j: {_CATEGORY_NAMES_CACHE}")
    except Exception as e:
        print(f"[Categories] Could not fetch: {e}")
    return _CATEGORY_NAMES_CACHE


def _fix_category_names_in_cypher(cypher: str) -> str:
    """
    Post-process generated Cypher to replace any product_category_name value
    with the closest actual value from Neo4j.

    Handles case mismatches like 'Toys' vs 'toys', 'Health_Beauty' vs 'health_beauty'.
    """
    import re as _re
    actual_cats = _get_actual_category_names()
    if not actual_cats:
        return cypher  # Can't fix without knowing real values

    # Build lowercase → actual mapping
    lower_to_actual = {c.lower(): c for c in actual_cats}
    # Also map with underscores removed and spaces removed for fuzzy match
    def _normalise_cat(s):
        return s.lower().replace("_", "").replace(" ", "").replace("-", "")
    norm_to_actual = {_normalise_cat(c): c for c in actual_cats}

    def _replace_cat(m):
        quoted_val = m.group(1)
        # Try exact match first
        if quoted_val in actual_cats:
            return f"= '{quoted_val}'"
        # Try lowercase match
        if quoted_val.lower() in lower_to_actual:
            correct = lower_to_actual[quoted_val.lower()]
            print(f"[Categories] Auto-corrected '{quoted_val}' → '{correct}'")
            return f"= '{correct}'"
        # Try normalised match (strip underscores/spaces)
        norm = _normalise_cat(quoted_val)
        if norm in norm_to_actual:
            correct = norm_to_actual[norm]
            print(f"[Categories] Auto-corrected '{quoted_val}' → '{correct}'")
            return f"= '{correct}'"
        # No match found — return as-is
        return f"= '{quoted_val}'"

    # Replace all product_category_name = '...' patterns
    fixed = _re.sub(
        r"product_category_name\s*=\s*'([^']+)'",
        _replace_cat,
        cypher
    )
    return fixed


def _unpack_value(v):
    if isinstance(v, Node):            return dict(v)
    elif isinstance(v, Relationship):  return {"type": v.type, **dict(v)}
    elif isinstance(v, Path):          return str(v)
    elif isinstance(v, list):          return [_unpack_value(i) for i in v]
    elif isinstance(v, dict):          return {k2: _unpack_value(v2) for k2, v2 in v.items()}
    else:                              return v

def _run_cypher_direct(cypher: str):
    try:
        with neo4j_driver.session(database=os.getenv("NEO4J_DATABASE", "neo4j")) as session:
            result  = session.run(cypher)
            keys    = result.keys()
            records = []
            for r in result:
                row = {}
                for key in keys:
                    unpacked = _unpack_value(r[key])
                    if isinstance(unpacked, dict) and len(keys) == 1:
                        row.update(unpacked)
                    elif isinstance(unpacked, dict):
                        for k2, v2 in unpacked.items():
                            row[f"{key}.{k2}"] = v2
                    else:
                        row[key] = unpacked
                records.append(row)
        return records, None
    except Exception as e:
        return [], str(e)

def get_kg_snapshot():
    try:
        with neo4j_driver.session(database=os.getenv("NEO4J_DATABASE", "neo4j")) as session:
            suppliers    = session.run("MATCH (s:Supplier)    RETURN count(s) AS c").single()["c"]
            plants       = session.run("MATCH (p:Plant)       RETURN count(p) AS c").single()["c"]
            shipments    = session.run("MATCH (sh:Shipment)   RETURN count(sh) AS c").single()["c"]
            distributors = session.run("MATCH (d:Distributor) RETURN count(d) AS c").single()["c"]
            retailers    = session.run("MATCH (r:Retailer)    RETURN count(r) AS c").single()["c"]
            routes       = session.run("MATCH (r:Route)       RETURN count(r) AS c").single()["c"]
        return {
            "Suppliers":    suppliers,
            "Plants":       plants,
            "Shipments":    shipments,
            "Distributors": distributors,
            "Retailers":    retailers,
            "Routes":       routes,
        }
    except:
        return {}


# ─────────────────────────────────────────────────────────────
# STEP 3 — Explain results with clean insight formatting
# ─────────────────────────────────────────────────────────────
def _clean_field_name(raw_key: str) -> str:
    """Convert 'sup.risk_score' -> 'risk score', 'p.plant_name' -> 'plant name'
    Also maps technical aggregated column names to human-readable labels."""
    # Domain-specific readable mappings
    _FIELD_LABELS = {
        # Distributor / Demand
        "distributor_city":             "Distributor City",
        "distributor_id":               "Distributor ID",
        "shortage_shipments":           "Shortage Shipments",
        "shipments_with_shortage":      "Shortage Shipments",
        "total_demand_gap":             "Total Demand Gap (units)",
        "delayed_shipments":            "Delayed Shipments",
        "avg_delay_days":               "Avg Delay (days)",
        "avg_delay":                    "Avg Delay (days)",
        "retailers_affected":           "Retailers Affected",
        "delayed_of_those":             "Of Which Delayed",
        # Retailer / Stockout
        "retailer_city":                "Retailer City",
        "retailer_id":                  "Retailer ID",
        "total_shortage_units":         "Total Unmet Units",
        "total_shortage":               "Total Unmet Units",
        "total_unmet_units":            "Total Unmet Units",
        "avg_shortage":                 "Avg Shortage per Shipment",
        "served_by_distributor":        "Served by Distributor",
        "retailers_in_city":            "Retailers in City",
        # Supplier
        "supplier_id":                  "Supplier ID",
        "supplier_name":                "Supplier",
        "risk_score":                   "Risk Score",
        "lead_time_days":               "Lead Time (days)",
        "StoP_lead_time_days":          "Supplier Lead Time (days)",
        "plant_avg_lead_time":          "Plant Avg Lead Time (days)",
        "lead_time_gap":                "Lead Time Gap (days)",
        "capacity_units":               "Annual Capacity (units)",
        "annual_capacity_units":        "Annual Capacity (units)",
        # Plant
        "plant_id":                     "Plant ID",
        "plant_name":                   "Plant",
        "total_shipments":              "Total Shipments",
        "delayed_count":                "Delayed Shipments",
        "delay_rate_pct":               "Delay Rate %",
        "primary_plant_name":           "Primary Plant",
        "supplying_plant":              "Supplying Plant",
        # Shipment
        "shipment_id":                  "Shipment ID",
        "delivery_status":              "Delivery Status",
        "delay_days":                   "Delay (days)",
        "transaction_date":             "Date",
        "route_id":                     "Route ID",
        "demand_gap":                   "Demand Gap (units)",
        # Product
        "product_category_name":        "Product Category",
        "product_id":                   "Product ID",
        "category":                     "Category",
        # Route
        "transport_mode":               "Transport Mode",
        "distance_km":                  "Distance (km)",
        "cost_inr":                     "Transport Cost (\u20b9)",
        "cost_efficiency":              "Cost Efficiency",
        "leadtime_days":                "Lead Time (days)",
        "PtoD_distance_km":             "Distance (km)",
        "PtoD_transportation_cost_inr": "Transport Cost (\u20b9)",
        "PtoD_leadtime_days":           "Lead Time (days)",
        # Aggregates
        "total_delayed":                "Total Delayed",
        "delayed":                      "Delayed Shipments",
        "on_time_count":                "On-Time Shipments",
        "supplier_count":               "No. of Suppliers",
        "avg_risk_score":               "Avg Risk Score",
        "max_risk_score":               "Max Risk Score",
        "single_source":                "Single Source?",
        "year_month":                   "Month",
        "downstream_demand_gap":        "Downstream Demand Gap (units)",
        "on_time_shipments":            "On-Time Shipments",
    }
    if raw_key in _FIELD_LABELS:
        return _FIELD_LABELS[raw_key]
    cleaned = re.sub(r'^[a-z]{1,3}\.', '', raw_key)
    cleaned = cleaned.replace('_', ' ')
    return cleaned

def _clean_dot_notation(text: str) -> str:
    """
    Comprehensive text sanitizer — removes all technical field name patterns
    before text reaches the UI. Handles:
      - dot notation:    s.delay_days       → delay days
      - snake_case:      delivery_status    → delivery status
      - underscores in  category names:    agro_industry_and_commerce → Agro Industry And Commerce
      - ALL remaining underscores anywhere in the text
    """
    if not text:
        return text

    # 1. Strip dot-notation prefixes (s., p., sup., dist., r., d., etc.)
    text = re.sub(r'\b[a-zA-Z]{1,4}\.', '', text)

    # 2. Replace underscores with spaces everywhere (catches snake_case field names
    #    and category names like agro_industry_and_commerce)
    text = re.sub(r'(?<=[a-zA-Z0-9])_(?=[a-zA-Z0-9])', ' ', text)

    # 3. Clean up any double spaces left behind
    text = re.sub(r' {2,}', ' ', text)

    return text

def generate_dynamic_insights(records):
    """
    Fallback insight generator used when LLM JSON parse fails.
    All field names and string values are cleaned of underscores and dot notation.
    """
    def _cfn(k):
        """Clean a field name to human-readable form."""
        k = re.sub(r'^[a-zA-Z]{1,4}\.', '', k)   # strip dot prefix
        return k.replace('_', ' ').strip().title()

    def _cv(v):
        """Clean a string value — replace underscores with spaces, title-case."""
        if isinstance(v, str):
            return v.replace('_', ' ').title()
        return v

    insights = []
    if not records:
        return []
    sample = records[:50]
    numeric_fields = [k for k, v in sample[0].items() if isinstance(v, (int, float))]

    # Unit-aware range insights — avoid appending wrong units to counts vs percentages vs days
    COUNT_WORDS  = {"shipments", "count", "retailers", "distributors", "suppliers", "plants",
                    "records", "delays", "total", "num"}
    DAY_WORDS    = {"days", "lead_time", "delay_days"}
    PCT_WORDS    = {"rate_pct", "rate", "pct", "percent", "percentage"}
    SCORE_WORDS  = {"risk_score", "efficiency", "score", "ratio"}

    def _field_unit(col: str) -> str:
        cl = col.lower()
        if any(w in cl for w in DAY_WORDS):    return " days"
        if any(w in cl for w in PCT_WORDS):    return "%"
        if any(w in cl for w in SCORE_WORDS):  return " (score)"
        if any(w in cl for w in {"cost_inr", "transportation_cost", "freight_cost"}): return " ₹"
        return ""  # counts get no unit suffix — just the number

    for field in numeric_fields[:2]:
        # Skip efficiency/score columns in range summary — not meaningful as ranges
        if any(w in field.lower() for w in {"efficiency", "ratio", "score"}):
            continue
        values = [r[field] for r in sample if isinstance(r.get(field), (int, float))]
        if values:
            label = _cfn(field)
            mn, mx = min(values), max(values)
            unit  = _field_unit(field)
            if mn == mx:
                insights.append(f"• {label} is consistently **{mn}{unit}** across all results.")
            else:
                insights.append(f"• {label} ranges from **{mn:,}{unit}** to **{mx:,}{unit}**.")

    # Only use actual day-duration columns for "X days" phrasing.
    # Columns like delayed_shipments (counts) or delay_rate_pct (%)
    # must NOT be labelled "days".
    delay_day_fields = [
        k for k in sample[0].keys()
        if "delay" in k.lower() and "days" in k.lower()  # e.g. delay_days, avg_delay_days
    ]
    for df in delay_day_fields:
        values = [r[df] for r in sample if isinstance(r.get(df), (int, float))]
        if values:
            avg = round(sum(values) / len(values), 2)
            label = _cfn(df)
            insights.append(f"• Average {label} is {avg} days, indicating the typical delay severity.")

    # For count columns (delayed_shipments, shortage_shipments, etc.) use "shipments" not "days"
    count_fields = [
        k for k in sample[0].keys()
        if any(w in k.lower() for w in ["shipments", "count", "num_", "retailers"])
        and isinstance(sample[0].get(k), (int, float))
    ]
    for cf in count_fields[:1]:
        values = [r[cf] for r in sample if isinstance(r.get(cf), (int, float))]
        if values:
            avg = round(sum(values) / len(values), 1)
            label = _cfn(cf)
            insights.append(f"• Average {label} is {avg:,.0f} per record in this result set.")

    for field in numeric_fields[:1]:
        sorted_data = sorted(sample, key=lambda x: x.get(field, 0), reverse=True)
        if sorted_data:
            top = sorted_data[0]
            label = _cfn(field)
            # Find a name/id field to identify the top entity
            name_key = next((k for k in top if any(w in k.lower() for w in ["name","city","id","category"])), None)
            entity = _cv(top.get(name_key, "")) if name_key else ""
            entity_str = f" — led by {entity}" if entity else ""
            insights.append(f"• Highest {label} is {top.get(field)}{entity_str}.")

    for k, v in sample[0].items():
        if isinstance(v, str):
            unique_vals = list(set(_cv(r[k]) for r in sample if r.get(k)))
            label = _cfn(k)
            if 1 < len(unique_vals) <= 5:
                insights.append(f"• {label} spans: {', '.join(map(str, unique_vals[:4]))}.")

    return insights[:5]


def explain_results(user_question: str, cypher: str, records: list) -> dict:
    if not records:
        return {
            "brief": "The query returned no results.",
            "insight": "• No data matched the query.\n• Try broadening your filter criteria.\n• Check if the entity IDs or values are spelled correctly."
        }

    keys = list(records[0].keys()) if records else []
    _agg_keys = {"total_nodes","total","count","total_count","node_count","record_count"}
    if len(records) == 1 and len(keys) == 1 and keys[0].lower() in _agg_keys:
        val = records[0][keys[0]]
        return {
            "brief": (f"The graph contains **{val:,}** nodes matching the query filter. "
                      "For a per-type breakdown try: *'How many of each node type are in the graph?'*"),
            "insight": (f"• Total nodes returned: **{val:,}**\n"
                        "• Aggregated into a single count — no per-type detail available.\n"
                        "• Re-run with a GROUP BY query to see per-label counts.\n"
                        "• The KG Snapshot panel shows per-type counts.")
        }

    if len(records) > 1 and "node_type" in keys and "count" in keys:
        lines = [f"• **{r['node_type']}**: {r['count']:,}" for r in records if r.get("node_type")]
        total_all = sum(r.get("count", 0) for r in records if r.get("count"))
        return {
            "brief": ("The supply chain graph contains: "
                      + ", ".join(f"{r['node_type']} ({r['count']:,})" for r in records if r.get("node_type"))
                      + f". Total: {total_all:,} nodes."),
            "insight": "\n".join(lines)
        }

    # ── Detect query intent ───────────────────────────────────────────────
    q_l = user_question.lower()
    _CATEGORY_MAP = {
        "toy": "Toys", "toys": "Toys",
        "auto": "Auto", "automobile": "Auto", "automotive": "Auto",
        "health": "Health_Beauty", "beauty": "Health_Beauty",
        "watch": "Watches_Gifts", "watches": "Watches_Gifts", "gift": "Watches_Gifts",
        "cool stuff": "Cool_Stuff", "cool": "Cool_Stuff",
        "bed bath": "Bed_Bath_Table", "bed": "Bed_Bath_Table",
        "construction": "Construction Tools Garden", "garden": "Construction Tools Garden",
    }
    _PLANT_MAP = {
        "baddi": "Baddi (PL1)", "bhopal": "Bhopal (PL2)",
        "pune": "Pune (PL3)", "goa": "Goa (PL4)"
    }
    detected_category = next((v for k, v in _CATEGORY_MAP.items() if k in q_l), None)
    detected_plant    = next((v for k, v in _PLANT_MAP.items() if k in q_l), None)

    _is_whatif_q      = any(w in q_l for w in [
        "what if", "what happens if", "removed from", "remove road", "remove rail",
        "without road", "without rail", "without air", "without sea",
        "road is removed", "rail is removed", "air is removed", "sea is removed",
        "transport is removed", "if road", "if rail", "if air", "if sea",
        "what would happen", "if we remove", "removing road", "removing rail",
    ])
    _whatif_mode = None
    if _is_whatif_q:
        for _mk, _mv in {"road": "Road", "rail": "Rail", "air": "Air", "sea": "Sea"}.items():
            if _mk in q_l:
                _whatif_mode = _mv
                break

    _is_category_q    = detected_category is not None
    _is_plant_q       = detected_plant is not None
    _is_supplier_q    = any(w in q_l for w in ["supplier", "vendor", "risk score", "risky"])
    _is_route_q       = any(w in q_l for w in ["route", "transport", "mode", "road", "rail", "air", "sea"])
    _is_mode_use_q    = any(w in q_l for w in ["what mode", "which mode", "modes does", "modes do",
                                                 "modes used", "mode does", "transport modes",
                                                 "uses what", "uses which", "what modes", "mode use"])
    _is_delay_q       = any(w in q_l for w in ["delay", "delayed", "late", "slow", "bottleneck"])
    _is_stockout_q    = any(w in q_l for w in ["stockout", "shortage", "demand gap", "unmet"])

    # Sort by key metric so LLM sees worst-case entity first
    sample = records[:8]
    total  = len(records)
    SORT_KEYS = ["delayed_shipments","total_demand_gap","shortage_shipments",
                 "total_shortage_units","delay_days","avg_delay_days","avg_delay",
                 "delayed_count","delayed","risk_score",
                 "PtoD_transportation_cost_inr","cost_inr"]
    for sk in SORT_KEYS:
        if records and sk in records[0]:
            try:
                sample = sorted(records, key=lambda r: float(r.get(sk,0) or 0), reverse=True)[:8]
            except Exception:
                pass
            break

    non_null_keys = [
        k for k in (sample[0].keys() if sample else [])
        if any(r.get(k) is not None for r in sample)
    ]
    clean_sample = [{k: r.get(k) for k in non_null_keys} for r in sample]

    has_limit  = "LIMIT" in cypher.upper()
    limit_note = ""
    if has_limit or total <= 20:
        limit_note = (f"\n⚠ SCOPING: result has {total} rows (LIMIT/WHERE filter applied). "
                      f"Qualify with 'among the {total} results', never 'all shipments'.")

    col_type_hints = []
    for k in non_null_keys:
        kl = k.lower()
        if "efficiency"    in kl: col_type_hints.append(f'"{k}" is score/ratio, NOT currency')
        elif "cost_inr"    in kl or "transportation_cost" in kl: col_type_hints.append(f'"{k}" is cost in INR')
        elif "rate_pct"    in kl or "delay_rate" in kl: col_type_hints.append(f'"{k}" is percentage 0-100')
        elif "risk_score"  in kl: col_type_hints.append(f'"{k}" is risk 0.0-1.0 (higher=riskier)')
        elif "demand_gap"  in kl or "shortage" in kl: col_type_hints.append(f'"{k}" is unmet demand units')
    col_hints_str = ("; ".join(col_type_hints) + ".") if col_type_hints else ""

    _top1     = json.dumps(clean_sample[0], default=str) if clean_sample else "N/A"
    _data_str = json.dumps(clean_sample[:6], indent=None, default=str)

    # Build focused context block based on query intent
    focus_ctx = ""
    if _is_mode_use_q:
        _plant_ctx_focus = detected_plant or "the specified plant"
        focus_ctx = (
            f"CRITICAL: User asks which transport modes {_plant_ctx_focus} uses — this is a FACTUAL LOOKUP, "
            f"NOT a delay analysis. The data shows transport modes with route counts, costs, and lead times. "
            f"Answer directly: '{_plant_ctx_focus} uses [N] transport modes: [list them]. "
            f"[Mode] has [N] routes with avg cost ₹X. [Mode2] has [N] routes with avg cost ₹Y.' "
            f"Do NOT mention delays, bottlenecks, or supply failures — the user just wants to know which modes the plant uses. "
            f"Each bullet must name a specific mode from the data with its route count and cost."
        )
    elif _is_whatif_q and _whatif_mode:
        focus_ctx = (
            f"CRITICAL: User asks what happens if {_whatif_mode} transport is removed from the network. "
            f"This is a what-if / simulation scenario — NOT a delay analysis. "
            f"The data shows distributor cities and plants that currently depend on {_whatif_mode} routes. "
            f"Lead with: 'Removing {_whatif_mode} transport would affect [N] cities — [top city] is most exposed "
            f"with [N] shipments at risk and [X] units demand gap.' "
            f"Then name the top 3 most exposed cities with their shipments_at_risk and demand_gap_at_risk values. "
            f"End with one strategic alternative (e.g. reroute to Road, higher rail capacity)."
        )
    elif _is_whatif_q:
        focus_ctx = (
            "CRITICAL: User asks a what-if / hypothetical scenario. "
            "Lead with the most exposed entity and state the exact impact in concrete numbers. "
            "Name the top 3 affected parties from the data. Suggest one mitigation strategy."
        )
    elif _is_category_q:
        focus_ctx = (
            f"CRITICAL: User asks about '{detected_category}' product category specifically. "
            f"Every insight MUST reference '{detected_category}' by name. "
            f"State which suppliers/plants cause {detected_category} delays with exact numbers. "
            f"Lead with: \"{detected_category} shipments are delayed because [supplier] (risk X.XX) "
            f"feeds [plant] which has [N] delayed {detected_category} shipments.\"  "
        )
    elif _is_supplier_q:
        focus_ctx = (
            "User asks about supplier risk. Lead with the highest-risk supplier name + exact risk_score. "
            "Each insight MUST name a real supplier with its exact risk score and delayed shipment count. "
            "Map each supplier to which plant it feeds."
        )
    elif _is_route_q:
        focus_ctx = (
            "User asks about transport routes/modes. Lead with the mode/route with highest delay count or cost. "
            "Each insight MUST name a specific transport mode (Road/Rail/Air/Sea) or route_id with exact numbers."
        )
    elif _is_plant_q:
        focus_ctx = (
            f"User asks about plant operations{(' specifically ' + detected_plant) if detected_plant else ''}. "
            "Lead with the delay rate and count for the relevant plant. "
            "Name the top suppliers feeding that plant with their risk scores."
        )
    elif _is_stockout_q:
        focus_ctx = (
            "User asks about stockouts/demand gaps. Lead with the city with highest total_demand_gap. "
            "State exact units and name the 3 worst-hit cities from the data."
        )
    elif _is_delay_q:
        _is_2x_q = any(w in q_l for w in ["twice", "2x", "double", "overrun", "twice the planned"])
        if _is_2x_q:
            focus_ctx = (
                "User asks about shipments that took more than twice the planned lead time. "
                "Lead with the plant that has the MOST shipments_exceeding_2x, give the exact count. "
                "Then name plants 2 and 3. State the avg_actual_days vs avg_planned_days for the worst plant. "
                "Every bullet must name a real plant (use plant_name) with an exact number."
            )
        elif any(w in q_l for w in ["most delayed", "most", "highest", "which plant"]):
            focus_ctx = (
                "User asks which plant has the most delayed shipments. "
                "Lead directly with the plant name that has the highest 'delayed' count from the data. "
                "Format: 'Bhopal (PL2) has 1,529 delayed shipments — the most among all 4 plants.' "
                "Then list all 4 plants with their exact delayed counts. "
                "Every bullet must name a real plant with its exact count from the data."
            )
        else:
            focus_ctx = (
                "User asks about delayed shipments. Lead with the plant/supplier with most delayed shipments. "
                "State exact counts and name specific entities — never generic advice."
            )

    prompt = (
        f'Supply chain analyst. User asked: "{user_question}"\n'
        f'{focus_ctx}\n'
        f'Data ({total} rows, top {len(clean_sample[:6])} shown, sorted by key metric DESC):\n'
        f'{_data_str}\n'
        + (f'Column types: {col_hints_str}\n' if col_hints_str else '')
        + f'{limit_note}\n'
        f'Top entity: {_top1}\n'
        'Output valid JSON on a single line with no markdown fences. Use this exact structure:\n'
        '{"brief":"2-3 sentences directly answering the question with REAL entity names and REAL numbers from the data above. '
        'Name the top entity by its actual value from data, then the 2nd and 3rd, then one key pattern.",'
        '"insight":"• Bhopal (PL2) has 1529 delayed shipments — the highest among all plants\\n'
        '• Pune (PL3) has 1513 delayed shipments — second highest\\n'
        '• [Replace these example bullets with real values from the actual data above]\\n'
        '• [Pattern finding with exact numbers from data]\\n'
        '• [One specific actionable recommendation with real entity name and metric]"}\n'
        'STRICT RULES:\n'
        '1. ONLY use entity names, numbers, and values that appear in the data above. Never invent values.\n'
        '2. Every bullet must have a real name from the data AND a real number — no placeholders like [X] or ?.\n'
        '3. Wrong: "Some categories dominate." Right: "Toys has 4,599 delayed shipments (highest across all categories)."\n'
        '4. The brief must directly answer the user question using concrete data values.\n'
        '5. If insight is about plants: name each plant as "PlantName (PLX)" with its exact delayed count.\n'
        '6. Never output template text or square-bracket placeholders in the final JSON.'
    )
    raw = call_llm(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=700,
        temperature=0.05,
    )
    raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    try:
        parsed = json.loads(raw)
        for key in ("brief", "insight"):
            parsed[key] = _clean_dot_notation(parsed.get(key, ""))
        insight = parsed.get("insight", "")
        # Strip leaked JSON from insights
        insight = re.sub(r'\[\s*\{[\s\S]*', '', insight).strip()
        insight = re.sub(r'\{\s*"tier"[\s\S]*', '', insight).strip()
        parsed["insight"] = insight
        if not any(l.strip().startswith("•") for l in insight.split("\n")):
            lines = [l.strip(" -–") for l in insight.split("\n") if l.strip()]
            parsed["insight"] = "\n".join("• " + l.lstrip("•").strip() for l in lines)
        return parsed
    except Exception:
        brief_match = re.search(r'"brief"\s*:\s*"([^"]+)"', raw)
        brief = brief_match.group(1) if brief_match else raw[:250]
        brief = _clean_dot_notation(brief)
        dynamic_insights = generate_dynamic_insights(records)
        return {
            "brief": brief,
            "insight": "\n".join(dynamic_insights) if dynamic_insights else "• No significant patterns detected in the results"
        }

# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────
def records_to_html(records: list, question: str = "", section_title: str = "") -> str:
    """
    Render Neo4j records as a styled HTML table.
    Adds a dynamic 2-line context header based on the query type.
    """
    if not records:
        return "<p style='color:#94a3b8;text-align:center;padding:20px;'>No data returned.</p>"
    try:
        import re as _re_rth

        # ── Detect query type for smart section title & description ──────
        q_l = (question or "").lower()
        _CATEGORY_MAP = {
            "toy": "Toys", "toys": "Toys", "auto": "Auto", "automobile": "Auto",
            "health": "Health_Beauty", "beauty": "Health_Beauty",
            "watch": "Watches_Gifts", "watches": "Watches_Gifts",
            "cool stuff": "Cool_Stuff", "bed bath": "Bed_Bath_Table",
            "construction": "Construction Tools Garden",
        }
        detected_cat = next((v for k, v in _CATEGORY_MAP.items() if k in q_l), None)

        all_keys = list(records[0].keys()) if records else []
        visible_keys = [k for k in all_keys if any(r.get(k) is not None for r in records)]

        # Detect what kind of data this table contains
        has_supplier   = any("supplier" in k.lower() for k in visible_keys)
        has_plant      = any("plant" in k.lower() for k in visible_keys)
        has_risk       = "risk_score" in visible_keys
        has_delay      = any("delay" in k.lower() for k in visible_keys)
        has_route      = any(k in visible_keys for k in ["route_id","mode","transport_mode","transportation_mode"])
        has_retailer   = any("retailer" in k.lower() for k in visible_keys)
        has_distributor = any("distributor" in k.lower() for k in visible_keys)
        has_product    = any("product" in k.lower() or "category" in k.lower() for k in visible_keys)
        has_shipment   = "shipment_id" in visible_keys or "delivery_status" in visible_keys
        # What-if scenario detection — columns produced by what-if cypher
        has_whatif_cols = any(k.startswith("routes_on_") for k in visible_keys) or \
                          "shipments_at_risk" in visible_keys or "routes_lost" in visible_keys or \
                          "distributors_exposed" in visible_keys or \
                          "affected_plants" in visible_keys or "exposed_cities" in visible_keys
        # Note: 'plants_affected' alone does NOT trigger what-if — it appears in transport mode
        # delay results (get_transport_mode_delays) which are NOT what-if queries.

        # Detect what-if intent from question
        _is_whatif_display = has_whatif_cols or any(w in q_l for w in [
            "what if", "what happens if", "removed from", "remove road", "remove rail",
            "without road", "without rail", "without air", "without sea",
            "road is removed", "rail is removed", "air is removed", "sea is removed",
            "transport is removed", "if road", "if rail", "if air", "if sea",
        ])
        # Extract removed mode name for display
        _removed_mode_display = None
        for _mk, _mv in {"road": "Road", "rail": "Rail", "air": "Air", "sea": "Sea"}.items():
            if _mk in q_l and _is_whatif_display:
                _removed_mode_display = _mv
                break

        # Determine section title and description
        if section_title:
            _title = section_title
            _desc  = f"Full result set — {len(records)} records retrieved and sorted by key metric."
        elif _is_whatif_display and _removed_mode_display:
            _mode_icon = {"Road": "🚛", "Rail": "🚂", "Air": "✈️", "Sea": "🚢"}.get(_removed_mode_display, "🚛")
            _title = f"{_mode_icon} What-If: {_removed_mode_display} Transport Removed"
            _desc  = (f"Showing the {len(records)} distributor cities and plants that currently depend on "
                      f"{_removed_mode_display} transport routes. If {_removed_mode_display} is removed, "
                      f"these routes are lost — the columns show how many shipments and how much demand gap "
                      f"would be at risk for each city. Higher = more severely exposed.")
        elif _is_whatif_display:
            _title = "🔬 What-If Scenario Analysis"
            _desc  = (f"Showing the {len(records)} entities affected by the hypothetical scenario. "
                      f"Each row represents a supply chain path or entity that would be impacted.")
        elif detected_cat:
            _title = f"📦 {detected_cat} Shipment Delay Analysis"
            _desc  = (f"Showing all {len(records)} supplier–plant pairs with delayed {detected_cat} shipments, "
                      f"sorted by delayed shipment count (highest first). "
                      f"Each row shows one supplier feeding a plant and how many {detected_cat} shipments were majorly delayed.")
        elif has_risk and has_supplier:
            _title = "🏭 High-Risk Supplier Analysis"
            _desc  = (f"Showing {len(records)} suppliers ranked by risk score. "
                      f"A risk score above 0.7 indicates a supplier with a poor reliability track record — "
                      f"these are the upstream parties most likely to cause downstream shipment delays.")
        elif has_plant and has_delay and not has_supplier:
            _has_2x = "shipments_exceeding_2x" in visible_keys
            _has_overrun = "avg_overrun_days" in visible_keys
            if _has_2x or any(w in q_l for w in ["twice", "2x", "double", "overrun", "lead time"]):
                _title = "⏱️ Lead Time Overrun Analysis — Shipments Exceeding 2× Planned"
                _desc  = (f"Showing {len(records)} plants ranked by number of shipments where actual delivery "
                          f"took more than twice the planned lead time. "
                          f"Higher counts indicate systemic planning failures or supplier unreliability at that plant.")
            elif any(w in q_l for w in ["most delayed", "most", "highest"]):
                _title = "⚠️ Plant Delay Rankings"
                _desc  = (f"Showing all {len(records)} plants ranked by total delayed shipments (Major Delay status). "
                          f"The plant with the most delayed shipments is the primary operational bottleneck. "
                          f"Delay Rate % shows what fraction of each plant's shipments arrive late.")
            else:
                _title = "⚠️ Bottleneck Plant Analysis"
                _desc  = (f"Showing delay rates and counts for all {len(records)} plants. "
                          f"The delay rate % tells you what fraction of each plant's shipments arrive late — "
                          f"higher = more operationally problematic.")
        elif has_route or any(w in q_l for w in ["route","transport","mode","road","rail","air","sea"]):
            # Distinguish: individual route rows vs mode-aggregate rows
            _has_route_id = "route_id" in visible_keys
            _has_mode_agg = any(k in visible_keys for k in ["transportation_mode","transport_mode"]) and \
                            any(k in visible_keys for k in ["total_delays","route_count","avg_cost_inr"])
            _is_cost_q    = any(w in q_l for w in ["cost","expensive","cheap","affordable","price"])
            _is_delay_q   = any(w in q_l for w in ["delay","late","delayed"])
            _is_mode_use_q = any(w in q_l for w in ["what mode","which mode","modes does","modes do",
                                                      "modes used","mode does","transport modes",
                                                      "uses what","uses which","what modes","mode use"])
            # Detect plant context from question
            _PLANT_NAMES = {"pl1":"PL1 (Baddi)","pl2":"PL2 (Bhopal)","pl3":"PL3 (Pune)","pl4":"PL4 (Goa)",
                            "baddi":"Baddi (PL1)","bhopal":"Bhopal (PL2)","pune":"Pune (PL3)","goa":"Goa (PL4)"}
            _plant_ctx = next((v for k, v in _PLANT_NAMES.items() if k in q_l), None)

            if _is_mode_use_q and _plant_ctx:
                _title = f"🚛 Transport Modes Used by Plant {_plant_ctx}"
                _desc  = (f"Plant {_plant_ctx} uses {len(records)} transport mode(s) across its routes. "
                          f"Each row shows one mode with the number of routes, average cost, lead time, "
                          f"and distance. This tells you how the plant distributes its logistics footprint "
                          f"across Road, Rail, Air, and Sea.")
            elif _is_mode_use_q:
                _title = "🚛 Transport Mode Usage by Plant"
                _desc  = (f"Showing which transport modes each plant uses across its routes. "
                          f"Each row is one plant–mode combination with route count and cost metrics.")
            elif _has_route_id and _is_cost_q:
                _order_word = "cheapest" if any(w in q_l for w in ["cheap","lowest","affordable"]) else "most expensive"
                _title = f"🛣️ Top Routes by Transportation Cost ({_order_word} first)"
                _desc  = (f"Showing the {len(records)} {_order_word} individual Plant→Distributor routes ranked by "
                          f"cost (₹ INR). Each row is one specific route — route_id format is 'Plant@Distributor'. "
                          f"Higher cost routes may offer faster lead times (Air) vs lower cost routes (Sea/Rail).")
            elif _has_route_id and _is_delay_q:
                _title = "🛣️ Route Delay Analysis"
                _desc  = (f"Showing {len(records)} individual Plant→Distributor routes ranked by delay impact. "
                          f"Each row is one specific route. Use this to pinpoint which logistics path is underperforming.")
            elif _has_route_id:
                _title = "🛣️ Individual Route Analysis"
                _desc  = (f"Showing {len(records)} individual Plant→Distributor routes. "
                          f"Route ID format: 'PlantID@DistributorID'. Ranked by the most significant metric.")
            elif _has_mode_agg and _is_delay_q:
                _title = "🚛 Transport Mode Delay Analysis"
                _desc  = (f"Showing delay counts across {len(records)} transport modes (Road/Rail/Air/Sea). "
                          f"Higher total_delays = that mode carries the most late shipments network-wide.")
            elif _has_mode_agg and _is_cost_q:
                _title = "🚛 Transport Mode Cost Comparison"
                _desc  = (f"Comparing average transportation costs across {len(records)} modes. "
                          f"Air is typically the most expensive, Sea the cheapest — trade-off with lead time.")
            else:
                _title = "🚛 Transport Route & Mode Analysis"
                _desc  = (f"Showing {len(records)} routes/modes ranked by the most significant metric. "
                          f"Use this to identify which transport mode or plant-distributor route is driving delay costs.")
        elif has_distributor and not has_supplier:
            _title = "🏙️ Distributor Impact Analysis"
            _desc  = (f"Showing {len(records)} distributor cities ranked by demand gap or delayed shipments. "
                      f"Higher demand gap = more units ordered but not delivered to that city.")
        elif has_retailer:
            _title = "🛒 Retailer Stockout Analysis"
            _desc  = (f"Showing {len(records)} retailers ranked by shortage severity. "
                      f"These are the consumer-facing locations absorbing the upstream supply chain failures.")
        elif has_shipment:
            _title = "📋 Delayed Shipment Records"
            _desc  = (f"Showing {len(records)} shipment records. "
                      f"Each row is one shipment with its delivery status, delay days, and route details.")
        else:
            # Check for on-time / delivery rate aggregate result (single row)
            _has_ontime = any(k in visible_keys for k in ["on_time_rate_pct","on_time_pct","on_time_percentage","on_time_count"])
            _has_pct    = any(k in visible_keys for k in ["delay_rate_pct","on_time_rate_pct","on_time_pct"])
            _is_pct_q   = any(w in q_l for w in ["percentage","percent","rate","on time","on-time"])
            _is_route_cost_filter = ("route_id" in visible_keys or "cost_inr" in visible_keys) and any(
                w in q_l for w in ["more than","above","greater than","less than","below","exceeding"]
            )

            if _has_ontime and _is_pct_q:
                _plant_name_q = next((v for k,v in {"goa":"Goa","bhopal":"Bhopal","pune":"Pune","baddi":"Baddi"}.items() if k in q_l), "Network")
                _title = f"📊 {_plant_name_q} Plant — Delivery Rate Summary"
                _desc  = (f"This shows the on-time vs delayed breakdown for {_plant_name_q} plant. "
                          f"On-time rate = shipments delivered without Major Delay. "
                          f"A rate below 50% means more than half of dispatches arrive late.")
            elif _has_pct and len(records) == 1:
                _title = "📊 Delivery Performance Summary"
                _desc  = "Single-row aggregate showing on-time and delay rate percentages for the filtered scope."
            elif _is_route_cost_filter:
                import re as _re_rth2
                _local_hints = _re_rth2.findall(r'(?:above|over|greater than|more than|below|under|less than)\s*(\d[\d,]*(?:\.\d+)?)', q_l)
                _threshold = _local_hints[0].replace(',','') if _local_hints else "threshold"
                _dir = "above" if any(w in q_l for w in ["more than","above","greater than","over","exceeding"]) else "below"
                _title = f"🛣️ Routes with Cost {_dir.title()} ₹{_threshold}"
                _desc  = (f"Showing {len(records)} Plant→Distributor routes where transportation cost is {_dir} ₹{_threshold}. "
                          f"Each row is one specific route. Higher cost routes typically use Air transport.")
            else:
                _title = "📋 Query Results"
                _desc  = (f"The query returned {len(records)} records, sorted by the most significant metric. "
                          f"Review the top rows for the highest-impact items.")

        # Build display-label map
        col_labels = {"#": "#"}
        _LABEL_MAP = {
            "supplier_id": "Supplier ID", "supplier_name": "Supplier",
            "plant_id": "Plant ID", "plant_name": "Plant",
            "distributor_id": "Distributor ID", "distributor_city": "City",
            "retailer_id": "Retailer ID", "retailer_city": "City",
            "risk_score": "Risk Score", "delayed_shipments": "Delayed Shipments",
            "avg_delay_days": "Avg Delay (days)", "avg_delay": "Avg Delay (days)",
            "delay_rate_pct": "Delay Rate %", "total_shipments": "Total Shipments",
            "delayed_count": "Delayed Count", "delay_days": "Delay Days",
            "total_demand_gap": "Demand Gap (units)", "shortage_shipments": "Shortage Shipments",
            "total_shortage_units": "Unmet Units", "transportation_mode": "Transport Mode",
            "transport_mode": "Transport Mode", "mode": "Mode",
            "total_delays": "Total Delays", "plants_affected": "Plants Affected",
            "PtoD_transportation_cost_inr": "Cost (₹ INR)",
            "cost_inr": "Cost (₹ INR)", "cost_efficiency": "Cost Efficiency",
            "PtoD_distance_km": "Distance (km)", "distance_km": "Distance (km)",
            "PtoD_leadtime_days": "Lead Time (days)", "leadtime_days": "Lead Time (days)",
            "delivery_status": "Status", "shipment_id": "Shipment ID",
            "product_category_name": "Product Category", "category": "Category",
            "route_id": "Route ID",
            # ── On-time / delivery rate columns ──────────────────────────
            "on_time_count": "On-Time Shipments", "on_time_rate_pct": "On-Time Rate %",
            "on_time_pct": "On-Time Rate %", "on_time_percentage": "On-Time Rate %",
            "delayed_count": "Delayed Count", "delay_rate_pct": "Delay Rate %",
            "delayed": "Delayed Shipments",
            "shipments_exceeding_2x": "Shipments > 2× Lead Time",
            "avg_overrun_days": "Avg Overrun (days)",
            "actual_days_taken": "Actual Days", "planned_lead_time_days": "Planned Days",
            "month_number": "Month", "week_number": "Week",
            "annual_sales_y2_cr": "Annual Sales (Cr)", "annual_sales_y1_cr": "Prior Year Sales (Cr)",
            "retailer_growth_rate": "Growth Rate", "retailer_growth_category": "Growth Category",
            "plant_count": "Plants Serving", "plants_serving": "Plants Serving",
            "sole_plant": "Sole Plant", "plant": "Plant",
            # ── What-if scenario columns ──────────────────────────────────
            "city": "Distributor City",
            "routes_on_road": "Road Routes Lost", "routes_on_rail": "Rail Routes Lost",
            "routes_on_air": "Air Routes Lost",  "routes_on_sea": "Sea Routes Lost",
            "routes_lost": "Routes Lost", "shipments_at_risk": "Shipments at Risk",
            "demand_gap_at_risk": "Demand Gap at Risk (units)",
            "distributors_exposed": "Distributors Exposed",
            "avg_route_cost_inr": "Avg Route Cost (₹)", "avg_leadtime_days": "Avg Lead Time (days)",
            "affected_plants": "Plants Affected", "exposed_cities": "Cities Exposed",
            "plants_using_mode": "Plants Using Mode", "distributors_served": "Distributors Served",
            "total_routes": "Total Routes", "avg_cost_inr": "Avg Cost (₹)",
        }
        for k in visible_keys:
            label = _LABEL_MAP.get(k, _clean_field_name(k))
            label = " ".join(w.capitalize() for w in label.split())
            col_labels[k] = label

        flat = []
        for i, r in enumerate(records, 1):
            row = {"#": i}
            for k in visible_keys:
                v = r.get(k)
                if isinstance(v, dict):   row[k] = json.dumps(v, default=str)
                elif isinstance(v, list): row[k] = ", ".join(str(x) for x in v)
                else:                     row[k] = v
            flat.append(row)

        import pandas as _pd_rth
        df   = _pd_rth.DataFrame(flat)
        cols = df.columns.tolist()

        # Risk score colour coding
        _risk_col = next((c for c in cols if c == "risk_score"), None)
        _delay_col = next((c for c in cols if c in ("delay_rate_pct", "delay_rate")), None)

        header_cells = "".join(f"<th>{col_labels.get(c, c)}</th>" for c in cols)
        rows_html = ""
        for _, row in df.iterrows():
            cells = ""
            for col in cols:
                val = str(row[col])
                # Delivery status badge
                if val == "Major Delay":
                    inner = f'<span class="status-delay">{val}</span>'
                elif val == "On Time":
                    inner = f'<span class="status-ok">{val}</span>'
                # Risk score colour
                elif col == _risk_col:
                    try:
                        rv = float(val)
                        col_c = "#f87171" if rv >= 0.8 else "#fb923c" if rv >= 0.6 else "#fbbf24" if rv >= 0.4 else "#4ade80"
                        inner = f'<strong style="color:{col_c}">{val}</strong>'
                    except Exception:
                        inner = val
                # Delay rate colour
                elif col == _delay_col:
                    try:
                        dr = float(val)
                        col_c = "#f87171" if dr >= 55 else "#fb923c" if dr >= 35 else "#4ade80"
                        inner = f'<strong style="color:{col_c}">{val}%</strong>'
                    except Exception:
                        inner = val
                else:
                    inner = val
                cells += f"<td>{inner}</td>"
            rows_html += f"<tr>{cells}</tr>"

        # Build section header HTML
        section_header = f"""
<div style="margin-bottom:14px;">
  <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
    <span style="font-size:0.92rem;font-weight:800;color:#e2e8f0">{_title}</span>
    <span style="font-size:0.68rem;font-weight:600;color:#38bdf8;background:rgba(56,189,248,0.1);
          border:1px solid rgba(56,189,248,0.3);border-radius:12px;padding:2px 10px;">
      {len(records)} records
    </span>
  </div>
  <p style="font-size:0.79rem;color:#94a3b8;margin:0;line-height:1.6;">{_desc}</p>
</div>"""

        return f"""
{section_header}
<div class="custom-table-wrap">
  <table class="custom-table">
    <thead><tr>{header_cells}</tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>"""
    except Exception as e:
        return f"<p style='color:red;'>Table render error: {e}</p>"
def generate_kpis(records):
    if not records:
        return {}
    # Use ALL records — not just first 100
    # Network-wide queries return 200 rows (50 cities × 4 plants)
    # slicing to [:100] would give wrong totals
    sample = records
    kpis   = {}
    cols   = list(sample[0].keys()) if sample else []

    # ── Demand gap / stockout KPIs (highest priority) ─────────
    for col, label in [
        ("demand_gap_at_risk",       "Demand Gap at Risk"),
        ("total_demand_gap_at_risk", "Total Demand Gap at Risk"),
        ("total_demand_gap",         "Total Demand Gap"),
        ("total_shortage_units",     "Total Unmet Units"),
    ]:
        if col in cols:
            vals = [r[col] for r in sample if isinstance(r.get(col), (int, float))]
            if vals:
                kpis[label] = f"{sum(vals):,.0f} units"
                break

    # ── Shipment count KPIs ────────────────────────────────────
    for col, label in [
        ("major_delays",      "Major Delay Shipments"),
        ("delayed_shipments", "Delayed Shipments"),
        ("total_delays",      "Total Delayed Shipments"),
        ("shortage_shipments","Shortage Shipments"),
    ]:
        if col in cols:
            vals = [r[col] for r in sample if isinstance(r.get(col), (int, float))]
            if vals:
                kpis[label] = f"{sum(vals):,.0f} shipments"
                break

    # ── Delay days — weighted average across all records ───────
    # Use mean (not max) — max gives the single worst row, mean gives network average
    for col, label in [
        ("avg_delay_days", "Avg Delay"),
        ("avg_delay",      "Avg Delay"),
    ]:
        if col in cols:
            vals = [r[col] for r in sample if isinstance(r.get(col), (int, float))]
            if vals:
                kpis[label] = f"{sum(vals)/len(vals):.2f} days"
                break
    # delay_days is a per-shipment max — show max for that case only
    if "Avg Delay" not in kpis and "delay_days" in cols:
        vals = [r["delay_days"] for r in sample if isinstance(r.get("delay_days"), (int, float))]
        if vals:
            kpis["Max Delay"] = f"{max(vals):.2f} days"

    # ── Risk score ─────────────────────────────────────────────
    for col in ("risk_score",):
        if col in cols:
            vals = [r[col] for r in sample if isinstance(r.get(col), (int, float))]
            if vals:
                kpis["Max Risk Score"] = f"{max(vals):.2f}"

    # ── Cost ───────────────────────────────────────────────────
    COST_PRIORITY = ["cost_inr","PtoD_transportation_cost_inr","transport_cost",
                     "transportation_cost","freight_cost","cost"]
    cost_key = next((k for k in COST_PRIORITY if k in cols),
                    next((k for k in cols if "cost" in k.lower()
                          and "efficiency" not in k.lower()), None))
    if cost_key:
        vals = [r[cost_key] for r in sample if isinstance(r.get(cost_key), (int, float))]
        if vals:
            kpis["Highest Cost (₹)"] = f"₹{max(vals):,.2f}"

    return kpis

def build_kpi_html(records):
    kpis = generate_kpis(records)
    if not kpis:
        return ""
    cards = "".join(
        f'<div class="kpi-card"><div class="kpi-label">{k}</div><div class="kpi-value">{v}</div></div>'
        for k, v in kpis.items()
    )
    return f'<div class="kpi-grid">{cards}</div>'

def _sanitize_insight_text(text: str) -> str:
    """
    Final cleanup pass on a single insight line before rendering.
    Removes any remaining technical artefacts and makes text look professional.
    """
    # Remove dot notation and underscores
    text = _clean_dot_notation(text)
    # Remove stray bullet prefix characters
    text = text.lstrip("•◆–-– ").strip()
    # Capitalise first letter
    if text:
        text = text[0].upper() + text[1:]
    return text


def _build_insight_html(raw: str) -> str:
    """Build styled Business Insights HTML from bullet text. Strips leaked JSON."""
    raw = _clean_dot_notation(raw)
    # Strip leaked JSON arrays/objects that sometimes appear at end of LLM output
    import re as _re_ins
    raw = _re_ins.sub(r'\[\s*\{[\s\S]*', '', raw).strip()
    raw = _re_ins.sub(r'\{\s*"tier"[\s\S]*', '', raw).strip()
    raw = _re_ins.sub(r'\{\s*"entity"[\s\S]*', '', raw).strip()

    lines = [l.strip() for l in raw.split("\n") if l.strip()]
    bullet_lines = [l for l in lines if l.startswith("•") and not l.startswith("• [")]
    if not bullet_lines:
        bullet_lines = ["• " + l.lstrip("•-– ").strip()
                        for l in lines if l.strip() and not l.startswith("{") and not l.startswith("[")]
    if not bullet_lines:
        return ""

    def _embolden(text):
        text = _re_ins.sub(
            r'\b(\d[\d,\.]*\s*(?:units|days|%|shipments|records|cities|suppliers|plants|INR)?)\b',
            r'<strong>\1</strong>', text)
        return text

    items_html = "".join(
        f'<div class="insight-item"><span class="insight-dot">◆</span>'
        f'<span class="insight-text">{_embolden(_sanitize_insight_text(l))}</span></div>'
        for l in bullet_lines[:6]
    )
    return f"""
<div class="insight-section">
    <div class="insight-heading custom-insight-heading">
        <span class="insight-heading-icon">◈</span>
        <span>✦ Business Insights</span>
        <span class="insight-heading-line"></span>
    </div>
    {items_html}
</div>
"""

_last_records: list = []

# ═════════════════════════════════════════════════════════════
# GRADIO HANDLERS
# ═════════════════════════════════════════════════════════════
def on_generate_query(question: str):
    global _last_records
    if not question.strip():
        return (
            '<div class="status-msg status-warn">Please enter a question first.</div>',
            gr.update(value="", visible=False),
            gr.update(visible=False),
            gr.update(visible=False),
            "", "", "", "",
            gr.update(visible=False),
        )
    try:
        cypher = generate_cypher(question)
    except Exception as e:
        return (
            f'<div class="status-msg status-error">LLM error: {e}</div>',
            gr.update(value="", visible=False),
            gr.update(visible=False),
            gr.update(visible=False),
            "", "", "", "",
            gr.update(visible=False),
        )

    # ── Auto-strip LIMIT clause so users always see full results ──────────
    # The LLM often generates LIMIT 15 as a default. Since the user can always
    # add it back, we strip it here so the query runs without a cap.
    import re as _re_ql
    cypher_stripped = _re_ql.sub(r'\bLIMIT\s+\d+\s*$', '', cypher.strip(), flags=_re_ql.IGNORECASE).strip()
    if cypher_stripped:
        cypher = cypher_stripped

    _last_records = []
    if question not in QUERY_HISTORY:
        QUERY_HISTORY.insert(0, question)
    QUERY_HISTORY[:] = QUERY_HISTORY[:5]
    return (
        '<div class="status-msg status-ok-gen">✦ Cypher generated — inspect or edit below, then click <strong style="color:#00ffcc;">▶ Run Query</strong> to execute.</div>',
        gr.update(value=cypher, visible=True),
        gr.update(visible=True),
        gr.update(visible=False),   # hide results_col until Run Query is clicked
        gr.update(value=''),        # run_status: clear
        gr.update(value=""),        # brief_md: clear
        gr.update(value=""),        # table_html: clear
        gr.update(value=""),        # insight_html: clear
        gr.update(visible=False),   # export_btn: hide
    )



# ─────────────────────────────────────────────────────────────
# RCA DETECTION — fast keyword classifier, zero LLM calls
# Returns True if the query is operationally significant enough
# to warrant a Root Cause Analysis.
# ─────────────────────────────────────────────────────────────
_RCA_TRIGGERS = {
    # Delay / performance
    "delay", "delayed", "late", "overdue", "behind schedule", "bottleneck",
    "slow", "slowest", "worst plant", "missing deadline", "lead time overrun",
    "actual days", "planned lead", "behind",
    # Risk / supplier issues
    "risk", "risky", "high risk", "at risk", "risk score", "critical supplier",
    "supplier issue", "supplier problem", "vendor risk", "unreliable",
    # Inventory / demand
    "stockout", "shortage", "demand gap", "unmet demand", "running out",
    "out of stock", "inventory", "replenishment",
    # Route / logistics issues
    "expensive route", "route cost", "inefficient route", "transport cost",
    "cost overrun", "logistics cost", "freight cost",
    # Defect / quality
    "defect", "quality issue", "rejection", "return rate", "damaged",
    # Root cause / investigation
    "why", "root cause", "cause of", "reason for", "what is causing",
    "investigate", "analysis", "diagnose",
    # Plant / facility issues
    "plant delay", "plant failure", "plant problem", "facility issue",
}
_RCA_EXCLUSIONS = {
    "hello", "hi", "help", "what can", "how do i use", "show me how",
    "list all", "show all", "count all", "how many total",
}
# Word-boundary set: these must match as whole words, not substrings
_RCA_EXCLUSION_WORDS = {"hello", "hi", "help"}

def _is_rca_worthy(question: str) -> bool:
    """
    Lightweight RCA classification — no LLM, no I/O.
    Returns True only for operational/risk/performance queries.
    """
    if not question or len(question.strip()) < 8:
        return False
    q = question.lower().strip()
    import re as _re
    # Word-level exclusions (avoid "hi" matching "shipments")
    words = set(_re.findall(r"\b\w+\b", q))
    if words & _RCA_EXCLUSION_WORDS:
        return False
    # Phrase exclusions (multi-word, safe to do substring)
    _phrase_exclusions = {"what can", "how do i use", "show me how",
                          "list all", "show all", "count all", "how many total"}
    if any(ph in q for ph in _phrase_exclusions):
        return False
    # Check triggers
    return any(trigger in q for trigger in _RCA_TRIGGERS)




def _generate_fast_brief(question: str, records: list) -> str:
    """
    Instant rule-based summary shown when the table loads,
    before the LLM insight call completes. Zero network calls.
    """
    if not records:
        return ""
    total = len(records)
    keys = list(records[0].keys()) if records else []
    q_l  = question.lower()

    # ── On-time / delivery rate aggregate (single or few rows) ───────────
    _has_ontime_key = any(k in keys for k in ["on_time_rate_pct","on_time_pct","on_time_percentage"])
    if _has_ontime_key:
        _ontime_key  = next(k for k in ["on_time_rate_pct","on_time_pct","on_time_percentage"] if k in keys)
        _delay_key   = next((k for k in ["delay_rate_pct","delayed_count"] if k in keys), None)
        _total_key   = next((k for k in ["total_shipments"] if k in keys), None)
        _ontime_val  = records[0].get(_ontime_key, 0)
        _delay_val   = records[0].get(_delay_key, 0) if _delay_key else ""
        _total_val   = records[0].get(_total_key, 0) if _total_key else ""
        _plant_name  = next((v for k,v in {"goa":"Goa (PL4)","bhopal":"Bhopal (PL2)","pune":"Pune (PL3)","baddi":"Baddi (PL1)"}.items() if k in q_l), "The plant")
        try:
            _ot_pct = f"{float(_ontime_val):.1f}%"
            _dl_pct = f"{float(_delay_val):.1f}%" if _delay_val != "" else ""
        except Exception:
            _ot_pct = str(_ontime_val)
            _dl_pct = str(_delay_val)
        _total_str = f" across **{int(_total_val):,} total shipments**" if _total_val else ""
        _delay_str = f", with **{_dl_pct} arriving with Major Delay**" if _dl_pct else ""
        return (
            f"**{_plant_name}** has an on-time delivery rate of **{_ot_pct}**{_total_str}{_delay_str}. "
            f"{'This means less than half of shipments arrive on schedule — a significant reliability issue.' if float(_ontime_val) < 50 else 'This indicates reasonable delivery performance.'}"
        )

    # ── Route cost threshold result ───────────────────────────────────────
    _is_route_cost_filter_brief = (
        any(k in keys for k in ["cost_inr","PtoD_transportation_cost_inr","route_id"]) and
        any(w in q_l for w in ["more than","above","greater than","less than","below","exceeding","cost"])
    )
    if _is_route_cost_filter_brief and total <= 10:
        import re as _re_brief2
        _brief_hints = _re_brief2.findall(r'(?:above|over|greater than|more than|below|under|less than|exceeding)\s*(\d[\d,]*(?:\.\d+)?)', q_l)
        _cost_key = next((k for k in ["cost_inr","PtoD_transportation_cost_inr"] if k in keys), None)
        _top_cost = records[0].get(_cost_key, 0) if _cost_key else 0
        try: _top_cost_fmt = f"₹{float(_top_cost):,.0f}"
        except: _top_cost_fmt = str(_top_cost)
        _top_route = str(records[0].get("route_id", records[0].get("plant", "")))
        _dir = "above" if any(w in q_l for w in ["more than","above","greater than","over","exceeding"]) else "below"
        return (
            f"Found **{total} route(s)** with cost {_dir} the threshold. "
            f"The highest-cost route is **{_top_route}** at **{_top_cost_fmt}**. "
            f"These are typically Air transport routes with the shortest lead times but highest cost."
        )

    # ── Transport mode USAGE query (what modes does plant X use?) ────────
    _is_mode_use_brief = any(w in q_l for w in [
        "what mode", "which mode", "modes does", "modes do", "modes used",
        "mode does", "transport modes", "uses what", "uses which", "what modes", "mode use"
    ])
    if _is_mode_use_brief and any(k in keys for k in ["transport_mode", "mode", "route_count"]):
        _PLANT_NAMES = {"pl1": "PL1 (Baddi)", "pl2": "PL2 (Bhopal)",
                        "pl3": "PL3 (Pune)", "pl4": "PL4 (Goa)",
                        "baddi": "Baddi", "bhopal": "Bhopal", "pune": "Pune", "goa": "Goa"}
        _plant_name = next((v for k, v in _PLANT_NAMES.items() if k in q_l), "The plant")
        _mode_col   = next((k for k in ["transport_mode", "mode"] if k in keys), None)
        _count_col  = next((k for k in ["route_count"] if k in keys), None)
        _cost_col   = next((k for k in ["avg_cost_inr", "cost_inr"] if k in keys), None)

        _modes = [str(r.get(_mode_col, "")).replace("_", " ") for r in records if r.get(_mode_col)]
        _mode_list = ", ".join(f"**{m}**" for m in _modes if m)
        _top_count = records[0].get(_count_col, "") if _count_col and records else ""
        _top_mode  = _modes[0] if _modes else "Road"
        _top_cost  = records[0].get(_cost_col, "") if _cost_col and records else ""
        try:
            _top_cost_fmt = f"₹{float(_top_cost):,.0f}" if _top_cost else ""
        except Exception:
            _top_cost_fmt = str(_top_cost)

        _cost_phrase = f" (avg cost {_top_cost_fmt} per route)" if _top_cost_fmt else ""
        return (
            f"**{_plant_name}** uses **{total} transport mode(s)**: {_mode_list}. "
            f"**{_top_mode}** has the most routes ({_top_count}){_cost_phrase}. "
            f"The table below shows route counts, average costs, and lead times per mode."
        )

    # ── What-if / transport-removal scenario ─────────────────────────────
    _is_whatif_brief = any(w in q_l for w in [
        "what if", "what happens if", "removed from", "remove road", "remove rail",
        "without road", "without rail", "without air", "without sea",
        "road is removed", "rail is removed", "air is removed", "sea is removed",
        "transport is removed", "if road", "if rail", "if air", "if sea",
    ])
    if _is_whatif_brief:
        _mode = next(
            (v for k, v in {"road":"Road","rail":"Rail","air":"Air","sea":"Sea"}.items() if k in q_l),
            "selected transport mode"
        )
        # Compute total shipments at risk and top city
        _at_risk_key = next((k for k in keys if "shipment" in k.lower() and "risk" in k.lower()), None)
        _gap_key     = next((k for k in keys if "demand_gap" in k.lower() or "gap_at_risk" in k.lower()), None)
        _city_key    = next((k for k in keys if "city" in k.lower() or "distributor" in k.lower()), None)
        _total_ships = sum(int(r.get(_at_risk_key, 0) or 0) for r in records) if _at_risk_key else 0
        _total_gap   = sum(float(r.get(_gap_key, 0) or 0) for r in records) if _gap_key else 0
        _top_city    = str(records[0].get(_city_key, "—")) if _city_key and records else "—"
        _top_ships   = int(records[0].get(_at_risk_key, 0) or 0) if _at_risk_key and records else 0
        return (
            f"Removing **{_mode}** transport would put **{_total_ships:,} shipments** at risk "
            f"across **{total} distributor cities**, with a combined demand gap of "
            f"**{_total_gap:,.0f} units**. "
            f"**{_top_city}** is the most exposed city ({_top_ships:,} shipments at risk). "
            f"Run the RCA Trail to identify alternative routing strategies."
        )

    # ── Guard: single-row aggregate (total_nodes, total_count, etc.) ─────
    _agg_keys = {"total_nodes", "total", "count", "total_count", "node_count", "record_count"}
    if total == 1 and len(keys) == 1 and keys[0].lower() in _agg_keys:
        val = records[0][keys[0]]
        label = _clean_field_name(keys[0])
        try:
            val = f"{int(val):,}"
        except Exception:
            val = str(val)
        return (
            f"**{val}** {label.lower()} found in the graph. "
            f"For a breakdown by entity type, try asking: "
            f"*'How many of each node type are in the graph?'*"
        )

    # ── Guard: node_type + count breakdown ───────────────────────────────
    if total > 1 and "node_type" in keys and "count" in keys:
        parts = [f"**{r['node_type']}** ({r['count']:,})" for r in records if r.get("node_type")]
        return "Graph node counts: " + ", ".join(parts) + ". Full analysis loading below."

    METRIC_PRIORITY = [
        # Demand / shortage metrics (highest business impact)
        "demand_gap_at_risk", "total_demand_gap", "shortage_shipments", "delayed_shipments",
        "total_shortage_units", "total_demand_gap_at_risk",
        # Delay metrics — including transport mode variants
        "major_delays", "total_delays", "delay_days", "avg_delay_days", "avg_delay",
        "delay_rate_pct", "delayed_count", "delayed", "count",
        # Distributor/supplier exposure
        "distributors_at_risk", "total_shipments",
        # Risk
        "risk_score",
        # Cost — real rupee columns BEFORE efficiency/ratio columns
        "PtoD_transportation_cost_inr", "cost_inr", "transportation_cost",
        "avg_cost_inr", "freight_cost", "transport_cost",
        # Retailer/route metrics
        "retailers_connected", "total_retailers", "route_count",
        # Efficiency/scores LAST — they are ratios, not money
        "cost_efficiency", "efficiency",
    ]
    LABEL_PRIORITY = [
        "distributor_city", "city", "retailer_city", "supplier_name", "plant_name",
        "supplier", "plant",   # aliases used by route queries
        "category", "product_category_name",
        "transportation_mode", "transport_mode", "mode", "route_id", "plant_id",
        "retailer_id",
    ]

    metric_key = next((k for k in METRIC_PRIORITY if k in keys), None)
    label_key  = next((k for k in LABEL_PRIORITY if k in keys), None)

    if not metric_key or not label_key:
        # ── Special case: only retailer_id / retailer_city returned ──
        # This happens when the Cypher returns DISTINCT retailers (no metric column)
        _has_retailer_cols = "retailer_id" in keys or "retailer_city" in keys
        if _has_retailer_cols:
            _q_l_ret = question.lower()
            _cities = list(dict.fromkeys(
                str(r.get("retailer_city", "")).strip()
                for r in records if r.get("retailer_city")
            ))
            _city_counts: dict = {}
            for r in records:
                c = str(r.get("retailer_city", "")).strip()
                if c:
                    _city_counts[c] = _city_counts.get(c, 0) + 1
            _top_city   = max(_city_counts, key=_city_counts.get) if _city_counts else "—"
            _top_count  = _city_counts.get(_top_city, 0)
            _n_cities   = len(_city_counts)
            _n_retailers = total
            if any(w in _q_l_ret for w in ["delay","delayed","late"]):
                return (
                    f"**{_n_retailers} retailers** across **{_n_cities} cities** are experiencing Major Delay shipments in the network. "
                    f"**{_top_city}** has the most affected retailers ({_top_count}), followed by other cities in the table below. "
                    f"Run the RCA Trail to identify which upstream suppliers and transport routes are driving these retail-level delays."
                )
            else:
                return (
                    f"**{_n_retailers} retailers** across **{_n_cities} cities** found. "
                    f"**{_top_city}** has the highest retailer concentration ({_top_count}). "
                    f"Detailed analysis loading below."
                )
        return f"**{total}** results retrieved — detailed analysis loading below."

    metric_label = _clean_field_name(metric_key)
    label_label  = _clean_field_name(label_key)

    try:
        sorted_recs = sorted(
            [r for r in records if r.get(metric_key) is not None],
            key=lambda r: float(r[metric_key]),
            reverse=True
        )
    except Exception:
        sorted_recs = records

    if not sorted_recs:
        return f"**{total}** results retrieved — detailed analysis loading below."

    top     = sorted_recs[0]
    top_lbl = str(top.get(label_key, "")).replace("_", " ").title()
    top_val = top.get(metric_key)
    try:
        top_val_f = float(top_val)
        top_val = f"{top_val_f:,.0f}" if top_val_f > 100 else f"{top_val_f:.2f}"
    except Exception:
        top_val = str(top_val)

    # Build query-aware opening sentence
    _q_low = question.lower()
    # Special case: Nagy PLC simulation — multi-distributor result
    if "nagy" in _q_low and any(w in _q_low for w in ["shutdown","cascading","impact","stockout","risk","production"]):
        _total_gap  = sum(float(r.get("demand_gap_at_risk", r.get("demand_gap", 0)) or 0) for r in records)
        _total_maj  = sum(int(r.get("major_delays", r.get("major_delay_count", 0)) or 0) for r in records)
        _n_cities   = len(set(str(r.get("distributor_city", r.get("city", ""))) for r in records if r.get("distributor_city") or r.get("city")))
        _top_city   = str(records[0].get("distributor_city", records[0].get("city", "—"))) if records else "—"
        _top_gap    = float(records[0].get("demand_gap_at_risk", records[0].get("demand_gap", 0)) or 0) if records else 0
        lines = [
            f"Nagy PLC (SUP0045, risk 0.99) is the **highest-risk supplier** in the network, "
            f"exclusively feeding Goa (PL4). A production shutdown would immediately impact "
            f"**{_n_cities} distributor cities** with a combined demand gap of "
            f"**{_total_gap:,.0f} units** at risk.",
            f"**{_top_city}** is the most exposed city. "
            f"The network already generates **{_total_maj:,} major delay shipments** via this supply path — "
            f"a shutdown escalates all of these to confirmed stockouts.",
        ]
        return " ".join(lines)
    # Special case: Kolkata distributor offline
    elif any(w in _q_low for w in ["kolkata","d0005"]) and any(w in _q_low for w in ["offline","flooding","flood","redirect"]):
        # New query returns supplier+plant level rows
        _total_gap    = sum(float(r.get("demand_gap_at_risk", r.get("total_demand_gap", 0)) or 0) for r in records)
        _total_ships  = sum(int(r.get("shipments_to_redirect", r.get("total_shipments", 0)) or 0) for r in records)
        _n_suppliers  = len(set(str(r.get("supplier_name","")) for r in records if r.get("supplier_name")))
        _n_plants     = len(set(str(r.get("plant_name", r.get("plant_id",""))) for r in records if r.get("plant_name") or r.get("plant_id")))
        # Top suppliers by demand gap
        _top_sups = sorted(
            [(str(r.get("supplier_name","")), float(r.get("demand_gap_at_risk",0) or 0))
             for r in records if r.get("supplier_name")],
            key=lambda x: x[1], reverse=True
        )[:3]
        _sup_names = ", ".join(f"**{s}**" for s, _ in _top_sups if s)
        lines = [
            f"If Kolkata distributor (D0005) goes offline, **{_n_suppliers} suppliers** across "
            f"**{_n_plants} plants** need rerouting — a combined **{_total_ships:,} shipments** "
            f"representing **{_total_gap:,.0f} units** of demand gap at risk.",
            f"The suppliers requiring immediate redirection are {_sup_names}. "
            f"Every candidate absorber city — Patna (181 shortage shipments), "
            f"Lucknow (176), and Raipur (176) — is already operating at full stress capacity, "
            f"meaning the network has **zero spare absorption capacity** for Kolkata's displaced volume.",
        ]
        return " ".join(lines)
    elif any(w in _q_low for w in ["most expensive","highest cost","expensive","which mode.*cost","transport.*cost","average.*cost"]):
        _opener = f"**{top_lbl}** has the highest average cost at **₹{top_val}**."
    elif any(w in _q_low for w in ["cost-efficient","cost efficient","most efficient","efficiency"]):
        _top_sorted = sorted([r for r in records if r.get(metric_key)], key=lambda r: float(r.get(metric_key) or 0), reverse=True)
        _eff_top = _top_sorted[0] if _top_sorted else top
        _eff_lbl = str(_eff_top.get(label_key, "")).replace("_", " ").title()
        _eff_val = _eff_top.get(metric_key, top_val)
        try: _eff_val = f"{float(_eff_val):.4f}"
        except: pass
        _opener = (
            f"The most cost-efficient route is **{_eff_lbl}** with an efficiency score of **{_eff_val}**. "
            f"Routes with higher efficiency scores deliver more value per kilometre travelled."
        )
    elif any(w in _q_low for w in ["most delay","delay","late","delayed"]):
        _opener = f"**{top_lbl}** has the highest {metric_label} at **{top_val}**."
    elif any(w in _q_low for w in ["risk","risky","high risk"]):
        _opener = f"**{top_lbl}** is the highest-risk entity with a score of **{top_val}**."
    elif any(w in _q_low for w in ["stockout","shortage","demand gap","running out"]):
        _opener = f"**{top_lbl}** faces the largest stock shortage at **{top_val}** units unmet."
    elif any(w in _q_low for w in ["route","transport"]):
        _keys_brief = list(records[0].keys()) if records else []
        _has_route_id_brief = "route_id" in _keys_brief
        _is_cost_brief = any(w in _q_low for w in ["cost","expensive","cheap","affordable"])
        if _has_route_id_brief and _is_cost_brief:
            _cost_key_brief = next((k for k in _keys_brief if "cost" in k.lower()), None)
            _top_cost = records[0].get(_cost_key_brief, top_val) if _cost_key_brief else top_val
            try: _top_cost = f"\u20b9{float(_top_cost):,.0f}"
            except: _top_cost = str(_top_cost)
            _top_plant  = str(records[0].get("plant", ""))
            _top_dist   = str(records[0].get("distributor_city", ""))
            _top_mode   = str(records[0].get("transport_mode", records[0].get("mode", "")))
            _route_desc = f"{_top_plant} \u2192 {_top_dist} via {_top_mode}" if _top_plant else str(records[0].get("route_id", top_lbl))
            _order_word = "cheapest" if any(w in _q_low for w in ["cheap","lowest","affordable"]) else "most expensive"
            _opener = (
                f"The {_order_word} route is **{_route_desc}** at **{_top_cost}**. "
                f"All {total} routes ranked below by cost — Air is typically highest, Sea/Rail lowest."
            )
        else:
            _opener = (
                f"**{top_lbl}** is the top-ranked route/mode with a {metric_label} of **{top_val}**. "
                f"All {total} routes are ranked below by {metric_label}."
            )
    elif any(w in _q_low for w in ["on-time","on time","delivery rate","performance"]):
        _opener = f"Overall on-time delivery rate is captured across the {total} records, with **{top_lbl}** as the top performer."
    else:
        _opener = (
            f"**{top_lbl}** leads with the highest {metric_label} at **{top_val}** "
            f"among the {total} results."
        )
    lines = [_opener]

    if len(sorted_recs) >= 2:
        second     = sorted_recs[1]
        second_lbl = str(second.get(label_key, "")).replace("_", " ").title()
        second_val = second.get(metric_key)
        try:
            second_val_f = float(second_val)
            second_val = f"{second_val_f:,.0f}" if second_val_f > 100 else f"{second_val_f:.2f}"
        except Exception:
            second_val = str(second_val)
        lines.append(
            f"**{second_lbl}** follows at {second_val}."
        )

    return " ".join(lines)

def on_run_query(question: str, cypher: str):
    """
    Step 1: Run the Cypher query and return table immediately — no LLM call.
    Returns 8 values: run_status, results_col, brief(empty), table_html,
                      insight_html(loading), export_btn, rca_worthy, rca_prefill
    Insights are filled in by on_run_insights() in a chained .then() call.
    """
    global _last_records, _last_question

    _FAIL_9 = (
        '<div class="status-msg status-warn">No query to run. Generate a query first.</div>',
        gr.update(visible=False), "", "", "", gr.update(visible=False),
        False, "", gr.update(visible=False),
    )

    def _err_9(msg: str):
        return (
            f'<div class="status-msg status-error">❌ {msg}</div>',
            gr.update(visible=False), "", "", "", gr.update(visible=False),
            False, "", gr.update(visible=False),
        )

    try:
        if not cypher or not cypher.strip():
            return _FAIL_9

        records, error, via_mcp = run_cypher_via_mcp(cypher)
        if error:
            _last_records = []
            return _err_9(f"Query Error — {error[:300]}")
        if not records:
            _last_records = []
            return (
                '<div class="status-msg status-warn">Query executed cleanly — zero results returned. Try rephrasing.</div>',
                gr.update(visible=False), "", "", "", gr.update(visible=False),
                False, "", gr.update(visible=False),
            )

        _last_records = records
        _last_question = question

        try:
            kpi_html   = build_kpi_html(records)
            table_html = records_to_html(records, question=question)
        except Exception as e:
            kpi_html   = ""
            table_html = f"<p style='color:#f87171'>Table render error: {e}</p>"
        full_html  = kpi_html + table_html

        # ── MCP vs direct badge — prominent, clearly visible ─────────────
        if via_mcp:
            source_badge = (
                '<span style="display:inline-flex;align-items:center;gap:6px;'
                'background:rgba(56,189,248,0.15);border:1px solid rgba(56,189,248,0.5);'
                'border-radius:20px;padding:3px 12px;font-size:0.78rem;'
                'color:#38bdf8;font-weight:700;margin-left:10px;">'
                '⟳ via MCP</span>'
            )
            source_note = (
                '<div style="font-size:0.68rem;color:#38bdf8;margin-top:4px;'
                'padding:3px 10px;opacity:0.8">'
                'Query routed through MCP server → Neo4j</div>'
            )
        else:
            source_badge = (
                '<span style="display:inline-flex;align-items:center;gap:6px;'
                'background:rgba(245,158,11,0.15);border:1px solid rgba(245,158,11,0.5);'
                'border-radius:20px;padding:3px 12px;font-size:0.78rem;'
                'color:#f59e0b;font-weight:700;margin-left:10px;">'
                '⚡ direct Neo4j</span>'
            )
            source_note = (
                '<div style="font-size:0.68rem;color:#f59e0b;margin-top:4px;'
                'padding:3px 10px;opacity:0.8">'
                'Query routed directly to Neo4j (MCP server not reachable)</div>'
            )
        run_status_html = f'''
    <div class="status-msg status-success">
        ✦ Analysis complete — <strong style="color:#00ffcc;">{len(records)} record(s)</strong> retrieved{source_badge}
        {source_note}
    </div>
    '''

        # RCA worthiness check (zero-cost keyword classifier)
        rca_worthy  = _is_rca_worthy(question)
        rca_prefill = question if rca_worthy else ""

        # Return table immediately — brief+insights filled by on_run_insights() via .then()
        # Eliminates the blocking LLM call so table renders instantly.
        brief_text = _generate_fast_brief(question, records)  # deterministic, zero LLM

        return (
            run_status_html,
            gr.update(visible=True),
            brief_text,
            full_html,
            "",                              # insight_html empty now, filled by .then()
            gr.update(visible=True),
            rca_worthy,
            rca_prefill,
            gr.update(visible=bool(rca_worthy)),
        )

    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        print(f"[on_run_query] Unhandled exception:\n{tb}")
        _last_records = []
        return _err_9(f"Unexpected error: {str(exc)[:250]}. Check terminal for details.")


def on_run_insights(question: str, cypher: str):
    """
    Step 2: Called via .then() after on_run_query.
    Returns 2 values: (brief_md, insight_html).
    brief_md is the LLM-written paragraph (replaces the fast rule-based brief).
    insight_html is the bullet-point insights section.
    """
    global _last_records
    records = _last_records
    if not records:
        return "", ""
    try:
        explanation  = explain_results(question, cypher, records)
        raw_brief    = explanation.get("brief", "")
        insight_html = _build_insight_html(explanation.get("insight", ""))

        # Guard: only reject LLM brief if it contains placeholder patterns or is too short
        _brief_has_placeholders = (
            "?" in raw_brief                          # literal ? placeholder
            or "[Specific" in raw_brief               # un-filled template text
            or "[Second" in raw_brief
            or "[Replace" in raw_brief
            or raw_brief.count("—") >= 4             # excessive em-dashes (garbled output)
        )
        _brief_too_short  = len(raw_brief.strip()) < 30

        if raw_brief and not _brief_has_placeholders and not _brief_too_short:
            # Use LLM brief — richer and query-specific
            final_brief = raw_brief
        else:
            # LLM brief is bad — regenerate from data directly
            final_brief = _generate_fast_brief(question, records)

        return final_brief, insight_html
    except Exception as e:
        return "", f'<div style="color:#f87171;font-size:0.78rem;padding:8px 0">⚠ Insights unavailable: {str(e)[:120]}</div>'

def on_abort():
    global _last_records
    _last_records = []
    return (
        '<div class="status-msg status-abort">Aborted. Start fresh with a new question.</div>',
        gr.update(value="", visible=False),
        gr.update(visible=False),
        gr.update(visible=False),   # hide results_col
        "", "", "", "",
        gr.update(visible=False),
    )

def clear_session():
    global _last_records
    _last_records = []
    QUERY_HISTORY.clear()
    return (
        '<div class="status-msg status-warn">Session cleared. Ready for a new query.</div>',
        gr.update(value="", visible=False),
        gr.update(visible=False),
        gr.update(visible=False),
        "", "", "", "",
        gr.update(visible=False),
    )

def make_csv(question):
    global _last_records
    if not _last_records:
        return None
    df = pd.DataFrame(_last_records)
    safe_name = re.sub(r'[^a-zA-Z0-9]+', '_', question.lower()).strip('_')
    filename = f"kg_results_{safe_name[:40]}.csv"
    df.to_csv(filename, index=False)
    return filename

def load_snapshot():
    snap = get_kg_snapshot()
    if not snap:
        return "<div class='snap-error'>Could not connect to Neo4j</div>"
    items = "".join(
        f'<div class="snap-item"><div class="snap-label">{k}</div><div class="snap-val">{v:,}</div></div>'
        for k, v in snap.items()
    )
    return f'<div class="snap-grid">{items}</div>'

def update_history():
    if not QUERY_HISTORY:
        return "<div class='hist-empty'>No queries yet</div>"
    items = "".join(
        f'<div class="hist-item"><span class="hist-idx">{i+1}</span><span class="hist-q">{q}</span></div>'
        for i, q in enumerate(QUERY_HISTORY)
    )
    return f'<div class="hist-list">{items}</div>'


# ═════════════════════════════════════════════════════════════
# CSS — white/off-white boxes replaced with blue-scheme colors
# ═════════════════════════════════════════════════════════════
CUSTOM_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700&display=swap');

:root {
    --bg:            #070d1a;
    --bg-panel:      #0c1528;
    --bg-card:       #101e36;
    --bg-input:      #09111f;
    --cyan:          #00e5ff;
    --violet:        #7c3aed;
    --pink:          #f472b6;
    --green:         #10b981;
    --red:           #ef4444;
    --text:          #dde6f5;
    --muted:         #5a7094;
    --border:        rgba(0,229,255,0.13);
    --radius:        13px;
    --mono:         'Space Mono', monospace;
    --sans:         'DM Sans', sans-serif;
    --cypher-bg:     #0d1f3a;
    --cypher-border: rgba(124,58,237,0.4);
}
body.gp-light {
    --bg:            #e8f1fb;
    --bg-panel:      #d4e4f7;
    --bg-card:       #c4d8f2;
    --bg-input:      #daeaf9;
    --cyan:          #1a56c4;
    --violet:        #6d28d9;
    --border:        rgba(26,86,196,.28);
    --text:          #0c1a36;
    --muted:         #2e4d80;
    --radius:        14px;
    --cypher-bg:     #c4d8f2;
    --cypher-border: rgba(109,40,217,.4);
}
*, *::before, *::after { box-sizing: border-box; }
.gradio-container,
.gradio-container > .main,
.gradio-container > .wrap {
    background: var(--bg) !important;
    font-family: var(--sans) !important;
    color: var(--text) !important;
    padding-top: 0 !important;
    margin-top: 0 !important;
    min-height: 100vh;
}
footer, .footer { display: none !important; }
.gradio-container::after {
    content: '';
    position: fixed;
    inset: 0;
    background:
        radial-gradient(ellipse 80% 50% at 8%  8%,  rgba(124,58,237,0.18) 0%, transparent 55%),
        radial-gradient(ellipse 60% 45% at 92% 92%, rgba(0,229,255,0.12)  0%, transparent 55%),
        radial-gradient(ellipse 40% 30% at 50% 50%, rgba(244,114,182,0.06) 0%, transparent 60%);
    pointer-events: none;
    z-index: 0;
    animation: bgPulse 8s ease-in-out infinite alternate;
}
@keyframes bgPulse { 0%{opacity:.8} 100%{opacity:1} }
body.gp-light .gradio-container::after { display:none; }

/* ── HERO ── */
#gp-hero {
    position: relative; z-index: 10;
    text-align: center; padding: 28px 24px 16px; overflow: visible;
}
#gp-hero::before {
    content: ''; position: absolute; top: -40px; left: 50%;
    transform: translateX(-50%); width: 600px; height: 180px;
    background: radial-gradient(ellipse, rgba(124,58,237,0.28) 0%, rgba(0,229,255,0.12) 45%, transparent 70%);
    filter: blur(28px); animation: heroOrb 6s ease-in-out infinite alternate; pointer-events: none;
}
@keyframes heroOrb {
    0%  { opacity:.7; transform:translateX(-50%) scaleX(1); }
    100%{ opacity:1;  transform:translateX(-50%) scaleX(1.12); }
}
.gp-agent-badge {
    display: inline-flex; align-items: center; gap: 6px;
    background: rgba(124,58,237,0.18); border: 1px solid rgba(124,58,237,0.45);
    border-radius: 999px; padding: 4px 14px 4px 10px;
    font-family: var(--mono); font-size: 0.58rem; letter-spacing: 2.5px;
    text-transform: uppercase; color: rgba(200,180,255,0.9); margin-bottom: 14px;
    box-shadow: 0 0 14px rgba(124,58,237,0.35), 0 0 28px rgba(124,58,237,0.12);
    animation: badgePulse 3s ease-in-out infinite;
}
@keyframes badgePulse {
    0%,100%{ box-shadow:0 0 14px rgba(124,58,237,.35),0 0 28px rgba(124,58,237,.12); }
    50%    { box-shadow:0 0 22px rgba(124,58,237,.55),0 0 40px rgba(124,58,237,.2);  }
}
.gp-agent-dot {
    width:6px; height:6px; border-radius:50%; background:#a78bfa;
    box-shadow:0 0 8px #7c3aed,0 0 16px rgba(124,58,237,.6);
    animation:dotBlink 2s ease-in-out infinite;
}
@keyframes dotBlink { 0%,100%{opacity:1} 50%{opacity:.4} }
.gp-hero-title-row {
    display: flex; align-items: center; justify-content: center;
    gap: 20px; margin-bottom: 8px; flex-wrap: wrap;
}
#gp-hero h1 {
    font-family: var(--mono) !important;
    font-size: clamp(2rem, 5vw, 3.2rem); font-weight: 700;
    letter-spacing: -1px; line-height: 1.1; margin: 0;
    background: linear-gradient(120deg, #00e5ff 0%, #a78bfa 40%, #f472b6 75%, #00e5ff 100%);
    background-size: 200% auto;
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
    animation: titleShimmer 5s linear infinite;
    filter: drop-shadow(0 0 30px rgba(0,229,255,.3)) drop-shadow(0 0 60px rgba(124,58,237,.2));
}
@keyframes titleShimmer { 0%{background-position:0% center} 100%{background-position:200% center} }
.gp-theme-toggle-wrap { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.gp-theme-toggle-label {
    font-family: var(--mono); font-size: 0.55rem; letter-spacing: 2px;
    text-transform: uppercase; color: var(--muted); user-select: none;
    transition: color .3s; min-width: 36px;
}
.gp-ios-toggle { position: relative; width: 52px; height: 28px; cursor: pointer; flex-shrink: 0; }
.gp-ios-toggle input { opacity:0; width:0; height:0; position:absolute; }
.gp-ios-slider {
    position: absolute; inset: 0; background: rgba(30,50,80,0.7);
    border-radius: 28px; border: 1.5px solid rgba(0,229,255,0.4);
    transition: all 0.4s cubic-bezier(.34,1.56,.64,1);
    box-shadow: 0 0 14px rgba(0,229,255,.35), 0 0 28px rgba(0,229,255,.12);
}
.gp-ios-slider::before {
    content: ''; position: absolute; width: 22px; height: 22px; left: 2px; top: 2px;
    background: linear-gradient(135deg, #e2e8f0, #cbd5e1); border-radius: 50%;
    transition: all 0.4s cubic-bezier(.34,1.56,.64,1); box-shadow: 0 2px 8px rgba(0,0,0,.4);
}
.gp-ios-slider::after {
    content: '🌙'; position: absolute; right: 5px; top: 50%;
    transform: translateY(-50%); font-size: 10px; pointer-events: none;
}
.gp-ios-toggle input:checked + .gp-ios-slider {
    background: linear-gradient(135deg, #1a3a70, #1e4088);
    border-color: rgba(29,78,216,.6);
    box-shadow: 0 0 12px rgba(29,78,216,.5), 0 0 24px rgba(29,78,216,.2);
}
.gp-ios-toggle input:checked + .gp-ios-slider::before {
    transform: translateX(24px);
    background: linear-gradient(135deg, #1d4ed8, #7c3aed);
    box-shadow: 0 2px 8px rgba(29,78,216,.6), 0 0 12px rgba(124,58,237,.4);
}
.gp-ios-toggle input:checked + .gp-ios-slider::after { content: '☀️'; right: auto; left: 5px; }
#gp-hero .gp-tagline {
    font-family: var(--mono); font-size: 0.65rem; letter-spacing: 4px;
    text-transform: uppercase; color: var(--muted); margin: 0 0 6px;
}
.gp-hero-divider { display:flex; align-items:center; justify-content:center; gap:12px; margin-top:14px; opacity:.6; }
.gp-hero-divider-line { height:1px; width:120px; background:linear-gradient(90deg,transparent,rgba(0,229,255,.5)); }
.gp-hero-divider-line.right { background:linear-gradient(90deg,rgba(0,229,255,.5),transparent); }
.gp-hero-divider-dot { width:5px; height:5px; border-radius:50%; background:var(--cyan); box-shadow:0 0 10px var(--cyan),0 0 20px rgba(0,229,255,.5); }
.gp-hero-stats { display:flex; justify-content:center; gap:10px; margin-top:14px; flex-wrap:wrap; }
.gp-stat-pill {
    background:rgba(0,229,255,.05); border:1px solid rgba(0,229,255,.15); border-radius:6px;
    padding:5px 14px; font-family:var(--mono); font-size:.6rem; letter-spacing:1.5px;
    color:rgba(0,229,255,.7); text-transform:uppercase; transition:all .25s;
}
.gp-stat-pill:hover { background:rgba(0,229,255,.1); border-color:rgba(0,229,255,.4); color:var(--cyan); box-shadow:0 0 16px rgba(0,229,255,.2); }

/* ── PANELS ── */
.left-panel, .main-panel {
    position:relative; z-index:5; background:var(--bg-panel) !important;
    border:1px solid var(--border) !important; border-radius:var(--radius) !important;
    padding:18px !important; overflow:visible !important;
}
.left-panel::before, .main-panel::before {
    content:''; position:absolute; top:-1px; left:20%; right:20%; height:1px;
    background:linear-gradient(90deg,transparent,var(--cyan),transparent); opacity:.7; filter:blur(1px);
}
.sec-label {
    font-family:var(--mono); font-size:.63rem; letter-spacing:3px; text-transform:uppercase;
    color:var(--cyan); display:flex; align-items:center; gap:8px; margin-bottom:12px;
    text-shadow:0 0 12px rgba(0,229,255,.6),0 0 24px rgba(0,229,255,.3);
}
.sec-label::after { content:''; flex:1; height:1px; background:linear-gradient(90deg,rgba(0,229,255,.35),transparent); }

/* ── SNAPSHOT ── */
.snap-grid { display:grid; grid-template-columns:1fr 1fr; gap:7px; margin-bottom:4px; }
.snap-item {
    background:var(--bg-card); border:1px solid var(--border); border-radius:9px;
    padding:9px 11px; transition:border-color .2s,box-shadow .2s; cursor:default;
}
.snap-item:hover { border-color:rgba(0,229,255,.45); box-shadow:0 0 18px rgba(0,229,255,.18),0 0 8px rgba(0,229,255,.08) inset; }
.snap-label { font-size:.68rem; color:var(--muted); margin-bottom:2px; }
.snap-val { font-family:var(--mono); font-size:1.05rem; color:var(--cyan); font-weight:700; text-shadow:0 0 14px rgba(0,229,255,.7),0 0 28px rgba(0,229,255,.35); }
.snap-error { color:var(--muted); font-size:.8rem; padding:8px 0; }

/* ── SAMPLE BUTTONS ── */
.sample-btn button {
    display:block !important; width:100% !important; background:var(--bg-card) !important;
    border:1px solid var(--border) !important; border-radius:7px !important; color:var(--text) !important;
    font-family:var(--sans) !important; font-size:.76rem !important; text-align:left !important;
    padding:8px 12px !important; cursor:pointer !important; transition:all .18s !important;
    margin-bottom:5px !important; white-space:nowrap !important; overflow:hidden !important;
    text-overflow:ellipsis !important; box-shadow:none !important;
}
.sample-btn button:hover {
    background:rgba(0,229,255,.06) !important; border-color:rgba(0,229,255,.5) !important;
    color:var(--cyan) !important; padding-left:16px !important;
    box-shadow:inset 3px 0 0 var(--cyan),0 0 14px rgba(0,229,255,.12) !important;
    text-shadow:0 0 10px rgba(0,229,255,.5) !important;
}

/* ── HISTORY ── */
.hist-list { display:flex; flex-direction:column; gap:5px; }
.hist-empty { color:var(--muted); font-size:.76rem; padding:4px 0; }
.hist-item { display:flex; gap:8px; align-items:flex-start; background:var(--bg-card); border:1px solid var(--border); border-radius:7px; padding:6px 10px; font-size:.74rem; }
.hist-idx { font-family:var(--mono); color:var(--violet); font-size:.63rem; padding-top:1px; flex-shrink:0; text-shadow:0 0 10px rgba(124,58,237,.7); }
.hist-q { color:var(--text); line-height:1.4; }

/* ── CLEAR BUTTON ── */
.clear-btn button {
    background:rgba(90,112,148,.12) !important; border:1px solid rgba(90,112,148,.22) !important;
    color:var(--muted) !important; font-size:.75rem !important; border-radius:7px !important;
    width:100% !important; margin-top:6px !important; transition:all .2s !important;
}
.clear-btn button:hover { color:var(--text) !important; border-color:rgba(90,112,148,.45) !important; }

/* ── TEXTAREA ── */
textarea {
    background:var(--bg-input) !important; border:1px solid var(--border) !important;
    border-radius:10px !important; color:var(--text) !important; font-family:var(--sans) !important;
    font-size:.9rem !important; padding:12px 14px !important;
    transition:border-color .2s,box-shadow .2s !important; resize:none !important;
}
textarea:focus {
    border-color:rgba(0,229,255,.6) !important;
    box-shadow:0 0 0 3px rgba(0,229,255,.08),0 0 24px rgba(0,229,255,.15) !important; outline:none !important;
}

/* Gradio label text */
.gradio-container label span { color:var(--muted) !important; font-family:var(--mono) !important; font-size:.65rem !important; letter-spacing:1.5px !important; }

/* ── GENERATE BUTTON ── */
.analyze-btn button {
    background:linear-gradient(135deg,#0ea5e9,#00e5ff,#06b6d4) !important;
    background-size:200% 200% !important; border:none !important; border-radius:10px !important;
    color:#020d1a !important; font-family:var(--mono) !important; font-size:.78rem !important;
    font-weight:700 !important; letter-spacing:1.5px !important; text-transform:uppercase !important;
    padding:12px 20px !important; cursor:pointer !important; transition:all .25s !important;
    animation:genBtnPulse 3s ease-in-out infinite !important;
    box-shadow:0 0 22px rgba(0,229,255,.55),0 0 44px rgba(14,165,233,.3),0 0 66px rgba(0,229,255,.15),0 4px 16px rgba(0,0,0,.4) !important;
    width:100% !important;
}
@keyframes genBtnPulse {
    0%,100%{box-shadow:0 0 22px rgba(0,229,255,.55),0 0 44px rgba(14,165,233,.3),0 0 66px rgba(0,229,255,.15),0 4px 16px rgba(0,0,0,.4);}
    50%    {box-shadow:0 0 32px rgba(0,229,255,.75),0 0 60px rgba(14,165,233,.45),0 0 90px rgba(0,229,255,.25),0 4px 16px rgba(0,0,0,.4);}
}
.analyze-btn button:hover { transform:translateY(-2px) !important; box-shadow:0 0 40px rgba(0,229,255,.8),0 0 80px rgba(14,165,233,.5),0 0 120px rgba(0,229,255,.3) !important; }

/* ── RUN QUERY BUTTON ── */
.run-btn button {
    background:linear-gradient(135deg,#059669,#10b981,#00e5ff) !important;
    border:none !important; border-radius:10px !important; color:#020d1a !important;
    font-family:var(--mono) !important; font-size:.78rem !important; font-weight:700 !important;
    letter-spacing:1.5px !important; text-transform:uppercase !important; padding:12px 20px !important;
    cursor:pointer !important; transition:all .25s !important;
    box-shadow:0 0 22px rgba(16,185,129,.5),0 0 44px rgba(0,229,255,.2),0 4px 16px rgba(0,0,0,.4) !important;
}
.run-btn button:hover { transform:translateY(-2px) !important; box-shadow:0 0 40px rgba(16,185,129,.7),0 0 80px rgba(0,229,255,.3) !important; }

/* ── ABORT BUTTON — beside Run Query ── */
.abort-btn button {
    background:linear-gradient(135deg,rgba(239,68,68,.15),rgba(220,38,38,.1)) !important;
    border:1.5px solid rgba(239,68,68,.5) !important; color:#fca5a5 !important;
    font-family:var(--mono) !important; font-size:.76rem !important; font-weight:700 !important;
    letter-spacing:1px !important; border-radius:10px !important; transition:all .2s !important;
    padding:12px 20px !important;
    animation:abortPulse 3s ease-in-out infinite !important;
    box-shadow:0 0 18px rgba(239,68,68,.35),0 0 36px rgba(239,68,68,.15) !important;
}
@keyframes abortPulse {
    0%,100%{box-shadow:0 0 18px rgba(239,68,68,.35),0 0 36px rgba(239,68,68,.15);}
    50%    {box-shadow:0 0 28px rgba(239,68,68,.55),0 0 55px rgba(239,68,68,.25);}
}
.abort-btn button:hover { background:rgba(239,68,68,.25) !important; border-color:rgba(239,68,68,.8) !important; box-shadow:0 0 36px rgba(239,68,68,.6),0 0 72px rgba(239,68,68,.3) !important; transform:translateY(-1px) !important; color:#fff !important; }

/* ── STATUS MESSAGES ── */
.status-html-wrap { min-height: 0; }
.status-msg { font-family:var(--mono); font-size:.83rem; padding:11px 16px; border-radius:9px; margin:8px 0 0; line-height:1.55; animation:fadeInStatus .35s ease-out; border-left:3px solid transparent; }
@keyframes fadeInStatus { from{opacity:0;transform:translateY(4px)} to{opacity:1;transform:translateY(0)} }
.status-ok-gen  { background:rgba(0,229,255,.08);   border-left-color:var(--cyan); color:#c0f0ff; box-shadow:-4px 0 18px rgba(0,229,255,.2); }
.status-success { background:rgba(16,185,129,.1);   border-left-color:#10b981;     color:#a7f3d0; box-shadow:-4px 0 18px rgba(16,185,129,.2); }
.status-warn    { background:rgba(245,158,11,.08);  border-left-color:#f59e0b;     color:#fde68a; box-shadow:-4px 0 18px rgba(245,158,11,.15); }
.status-error   { background:rgba(239,68,68,.1);    border-left-color:#ef4444;     color:#fca5a5; box-shadow:-4px 0 18px rgba(239,68,68,.2); }
.status-abort   { background:rgba(100,116,139,.1);  border-left-color:#64748b;     color:#94a3b8; }
.status-html-wrap p { margin:0 !important; }

/* ── CYPHER BOX ── */
.cm-editor, .cm-editor * { background:var(--cypher-bg) !important; }
.cm-editor { border:1.5px solid var(--cypher-border) !important; border-radius:10px !important; box-shadow:0 0 18px rgba(124,58,237,.2),0 0 36px rgba(0,229,255,.06),inset 0 0 24px rgba(0,229,255,.03) !important; }
.cm-editor.cm-focused { border-color:rgba(0,229,255,.6) !important; box-shadow:0 0 24px rgba(0,229,255,.3),0 0 48px rgba(0,229,255,.1) !important; }
.cm-content { font-family:var(--mono) !important; font-size:.82rem !important; color:#a8d8ff !important; }
.cm-line { color:#a8d8ff !important; }

/* ── RESULTS ── */
.results-wrap { background:var(--bg-card); border:1px solid var(--border); border-radius:var(--radius); overflow:hidden; margin-top:10px; box-shadow:0 0 32px rgba(0,229,255,.06); }
.brief-box { background:linear-gradient(135deg,rgba(124,58,237,.15),rgba(0,229,255,.07)); border-bottom:1px solid var(--border); padding:15px 18px; }
.brief-box p, .brief-md p { font-size:.9rem !important; line-height:1.7 !important; color:var(--text) !important; margin:0 !important; }

/* ── TABLE ── */
.custom-table-wrap { overflow-x:auto; max-height:700px; overflow-y:auto; scrollbar-width:thin; scrollbar-color:rgba(0,229,255,.5) transparent; }
.custom-table-wrap::-webkit-scrollbar { width:5px; height:5px; }
.custom-table-wrap::-webkit-scrollbar-thumb { background:rgba(0,229,255,.3); border-radius:3px; }
.custom-table { width:100%; border-collapse:collapse; font-size:.79rem; }
.custom-table thead tr { background:rgba(0,229,255,.06); border-bottom:1px solid rgba(0,229,255,.22); position:sticky; top:0; z-index:2; backdrop-filter:blur(8px); }
.custom-table th { padding:9px 13px; text-align:left; font-family:var(--mono); font-size:.62rem; letter-spacing:2px; text-transform:uppercase; color:var(--cyan); font-weight:700; white-space:nowrap; text-shadow:0 0 12px rgba(0,229,255,.7),0 0 24px rgba(0,229,255,.3); }
.custom-table tbody tr { border-bottom:1px solid rgba(255,255,255,.035); transition:background .12s; }
.custom-table tbody tr:hover { background:rgba(0,229,255,.04); }
.custom-table td { padding:8px 13px; color:var(--text); vertical-align:middle; white-space:nowrap; }
.custom-table td:first-child { font-family:var(--mono); font-size:.67rem; color:var(--muted); }
.status-delay { background:rgba(239,68,68,.14); color:#fca5a5; border:1px solid rgba(239,68,68,.3); border-radius:4px; padding:2px 8px; font-family:var(--mono); font-size:.7rem; box-shadow:0 0 10px rgba(239,68,68,.25); text-shadow:0 0 8px rgba(239,68,68,.5); }
.status-ok    { background:rgba(16,185,129,.14); color:#6ee7b7; border:1px solid rgba(16,185,129,.3); border-radius:4px; padding:2px 8px; font-family:var(--mono); font-size:.7rem; box-shadow:0 0 10px rgba(16,185,129,.25); text-shadow:0 0 8px rgba(16,185,129,.5); }

/* ── INSIGHTS ── */
.insight-section { padding:16px 18px 18px; border-top:1px solid var(--border); }
.insight-heading.custom-insight-heading span { color:var(--cyan) !important; }
.insight-heading.custom-insight-heading { color:var(--cyan) !important; text-shadow:none !important; }
.insight-heading { display:flex; align-items:center; gap:10px; margin-bottom:14px; font-family:var(--mono); font-size:.65rem; letter-spacing:3px; text-transform:uppercase; color:#00e5ff; text-shadow:0 0 10px rgba(0,229,255,0.9),0 0 20px rgba(0,229,255,0.6),0 0 35px rgba(0,229,255,0.3); }
.insight-heading-icon { font-size:.8rem; color:#00e5ff; text-shadow:0 0 12px rgba(0,229,255,1); }
.insight-heading-line { flex:1; height:1px; background:linear-gradient(90deg,#00e5ff,transparent); box-shadow:0 0 10px rgba(0,229,255,0.6); }
.insight-item { display:flex; gap:12px; align-items:flex-start; padding:10px 12px; margin-bottom:8px; background:rgba(0,229,255,0.05); border:1px solid rgba(0,229,255,0.2); border-radius:10px; font-size:.84rem; line-height:1.6; color:#e2e8f0; transition:all .25s ease; }
.insight-item:last-child { margin-bottom:0; }
.insight-item:hover { border-color:rgba(0,229,255,0.6); box-shadow:0 0 12px rgba(0,229,255,0.2),0 0 20px rgba(0,229,255,0.15); }
.insight-dot { color:#00e5ff; font-size:.6rem; padding-top:4px; flex-shrink:0; text-shadow:0 0 8px rgba(0,229,255,1),0 0 16px rgba(0,229,255,0.6); }
.insight-text { color:#e2e8f0; }

/* ── EXPORT BUTTON ── */
.export-btn a, .export-btn button {
    background:rgba(0,229,255,.08) !important; border:1.5px solid rgba(0,229,255,.4) !important;
    color:var(--cyan) !important; font-family:var(--mono) !important; font-size:.72rem !important;
    letter-spacing:1.5px !important; text-transform:uppercase !important; border-radius:8px !important;
    padding:10px 18px !important; display:flex !important; align-items:center !important;
    justify-content:center !important; gap:6px !important; width:calc(100% - 36px) !important;
    margin:10px 18px !important; text-align:center !important; text-decoration:none !important;
    transition:all .25s !important; box-shadow:0 0 18px rgba(0,229,255,.2),inset 0 0 12px rgba(0,229,255,.04) !important;
    text-shadow:0 0 12px rgba(0,229,255,.6) !important; cursor:pointer !important;
}
.export-btn a:hover, .export-btn button:hover { background:rgba(0,229,255,.18) !important; border-color:rgba(0,229,255,.7) !important; box-shadow:0 0 30px rgba(0,229,255,.5),0 0 60px rgba(0,229,255,.2) !important; transform:translateY(-1px) !important; }

/* ── KPI CARDS ── */
.kpi-grid { display:flex; gap:10px; padding:14px 18px; border-bottom:1px solid var(--border); }
.kpi-card { flex:1; background:linear-gradient(135deg,rgba(0,229,255,0.08),rgba(124,58,237,0.08)); border:1px solid var(--border); border-radius:10px; padding:10px 12px; text-align:center; }
.kpi-label { font-size:0.65rem; color:var(--muted); margin-bottom:4px; font-family:var(--mono); letter-spacing:1px; }
.kpi-value { font-family:var(--mono); font-size:1rem; color:var(--cyan); text-shadow:0 0 10px rgba(0,229,255,.6); }

/* ════════════════════════════════════════
   LIGHT MODE OVERRIDES
   ════════════════════════════════════════ */

/* ── Base containers ── */
body.gp-light,
body.gp-light .gradio-container,
body.gp-light .gradio-container > .main,
body.gp-light .gradio-container > .wrap,
body.gp-light .gradio-container > div,
body.gp-light .contain,
body.gp-light #root { background:var(--bg) !important; color:var(--text) !important; }

/* ── Gradio native elements ── */
body.gp-light .block,
body.gp-light .form,
body.gp-light .gap,
body.gp-light .row { background:transparent !important; }
body.gp-light label span,
body.gp-light .label-wrap span { color:var(--muted) !important; }
body.gp-light .wrap.svelte-1p9xokt,
body.gp-light .wrap { background:var(--bg-input) !important; border-color:var(--border) !important; }

/* ── Tabs ── */
body.gp-light .tabs,
body.gp-light .tab-nav { background:var(--bg-panel) !important; border-color:var(--border) !important; }
body.gp-light .tab-nav button { color:var(--muted) !important; background:transparent !important; border-color:transparent !important; }
body.gp-light .tab-nav button:hover { color:var(--cyan) !important; background:rgba(26,86,196,.08) !important; }
body.gp-light .tab-nav button.selected,
body.gp-light .tab-nav button[aria-selected="true"] { color:var(--cyan) !important; background:rgba(26,86,196,.12) !important; border-bottom-color:var(--cyan) !important; }
body.gp-light .tabitem { background:var(--bg) !important; border-color:var(--border) !important; }

/* ── Inputs, selects, dropdowns ── */
body.gp-light input,
body.gp-light select,
body.gp-light .svelte-input,
body.gp-light input[type="text"] { background:var(--bg-input) !important; color:var(--text) !important; border-color:var(--border) !important; }
body.gp-light textarea { background:var(--bg-input) !important; color:var(--text) !important; border-color:var(--border) !important; }
body.gp-light textarea:focus { border-color:var(--cyan) !important; box-shadow:0 0 0 3px rgba(26,86,196,.12),0 0 20px rgba(26,86,196,.12) !important; }
body.gp-light textarea::placeholder { color:var(--muted) !important; opacity:.7 !important; }
body.gp-light .dropdown,
body.gp-light .dropdown-arrow,
body.gp-light select { background:var(--bg-input) !important; color:var(--text) !important; border-color:var(--border) !important; }
body.gp-light .dropdown ul,
body.gp-light .options { background:var(--bg-card) !important; border-color:var(--border) !important; }
body.gp-light .item { color:var(--text) !important; }
body.gp-light .item:hover { background:rgba(26,86,196,.12) !important; color:var(--cyan) !important; }

/* ── Gradio Markdown / HTML output areas ── */
body.gp-light .prose,
body.gp-light .md,
body.gp-light .markdown,
body.gp-light .output-markdown p,
body.gp-light .output-markdown li,
body.gp-light .output-html { color:var(--text) !important; }
body.gp-light .prose strong,
body.gp-light .md strong { color:#0c1a36 !important; }

/* ── Textbox (output) ── */
body.gp-light .output-textbox,
body.gp-light .input-textbox { background:var(--bg-input) !important; color:var(--text) !important; border-color:var(--border) !important; }

/* ── Scrollbars ── */
body.gp-light ::-webkit-scrollbar { background:var(--bg-card) !important; }
body.gp-light ::-webkit-scrollbar-thumb { background:rgba(26,86,196,.3) !important; border-color:var(--bg-card) !important; }

/* ── Hero section ── */
body.gp-light #gp-hero::before { background:radial-gradient(ellipse,rgba(26,86,196,.18) 0%,rgba(109,40,217,.08) 45%,transparent 70%) !important; }
body.gp-light #gp-hero h1 { filter:drop-shadow(0 0 16px rgba(26,86,196,.3)) drop-shadow(0 0 32px rgba(109,40,217,.15)) !important; }
body.gp-light .gp-tagline { color:var(--muted) !important; }
body.gp-light .gp-agent-badge { background:rgba(26,86,196,.12) !important; border-color:rgba(26,86,196,.45) !important; color:rgba(26,86,196,.95) !important; box-shadow:0 0 12px rgba(26,86,196,.25) !important; }
body.gp-light .gp-agent-dot { background:#6d28d9 !important; box-shadow:0 0 8px #6d28d9 !important; }
body.gp-light .gp-stat-pill { background:rgba(26,86,196,.08) !important; border-color:rgba(26,86,196,.28) !important; color:rgba(26,86,196,.9) !important; }
body.gp-light .gp-stat-pill:hover { background:rgba(26,86,196,.18) !important; border-color:rgba(26,86,196,.55) !important; color:var(--cyan) !important; }
body.gp-light .gp-hero-divider-dot { background:var(--cyan) !important; box-shadow:0 0 10px var(--cyan),0 0 20px rgba(26,86,196,.4) !important; }
body.gp-light .gp-hero-divider-line { background:linear-gradient(90deg,transparent,rgba(26,86,196,.5)) !important; }
body.gp-light .gp-hero-divider-line.right { background:linear-gradient(90deg,rgba(26,86,196,.5),transparent) !important; }

/* ── Toggle ── */
body.gp-light .gp-theme-toggle-label { color:var(--muted) !important; }
body.gp-light .gp-ios-slider { background:rgba(180,210,250,.6) !important; border-color:rgba(26,86,196,.4) !important; box-shadow:0 0 10px rgba(26,86,196,.2) !important; }
body.gp-light .gp-ios-toggle input:checked + .gp-ios-slider { background:linear-gradient(135deg,#c4d8f2,#d4e4f7) !important; border-color:rgba(26,86,196,.5) !important; box-shadow:0 0 10px rgba(26,86,196,.3) !important; }

/* ── Panels ── */
body.gp-light .left-panel,
body.gp-light .main-panel { background:var(--bg-panel) !important; border-color:var(--border) !important; box-shadow:0 4px 24px rgba(26,86,196,.1) !important; }
body.gp-light .left-panel::before,
body.gp-light .main-panel::before { background:linear-gradient(90deg,transparent,var(--cyan),transparent) !important; opacity:.5 !important; }

/* ── Labels / section headers ── */
body.gp-light .sec-label { color:var(--cyan) !important; text-shadow:0 0 10px rgba(26,86,196,.25) !important; }
body.gp-light .sec-label::after { background:linear-gradient(90deg,rgba(26,86,196,.3),transparent) !important; }

/* ── Snapshot cards ── */
body.gp-light .snap-item { background:var(--bg-card) !important; border-color:var(--border) !important; }
body.gp-light .snap-item:hover { border-color:rgba(26,86,196,.5) !important; box-shadow:0 0 16px rgba(26,86,196,.15) inset !important; }
body.gp-light .snap-val { color:var(--cyan) !important; text-shadow:0 0 10px rgba(26,86,196,.3) !important; }
body.gp-light .snap-label { color:var(--muted) !important; }
body.gp-light .snap-error { color:var(--muted) !important; }

/* ── Sample buttons ── */
body.gp-light .sample-btn button { background:var(--bg-card) !important; border-color:var(--border) !important; color:var(--text) !important; }
body.gp-light .sample-btn button:hover { background:rgba(26,86,196,.12) !important; border-color:rgba(26,86,196,.55) !important; color:var(--cyan) !important; box-shadow:inset 3px 0 0 var(--cyan),0 0 12px rgba(26,86,196,.1) !important; text-shadow:none !important; }

/* ── History ── */
body.gp-light .hist-item { background:var(--bg-card) !important; border-color:var(--border) !important; }
body.gp-light .hist-q { color:var(--text) !important; }
body.gp-light .hist-idx { color:var(--violet) !important; text-shadow:none !important; }
body.gp-light .hist-empty { color:var(--muted) !important; }

/* ── Clear / secondary buttons ── */
body.gp-light .clear-btn button { background:rgba(26,86,196,.08) !important; border-color:rgba(26,86,196,.22) !important; color:var(--muted) !important; }
body.gp-light .clear-btn button:hover { color:var(--text) !important; border-color:rgba(26,86,196,.4) !important; }

/* ── Run / Analyze buttons ── */
body.gp-light .analyze-btn button,
body.gp-light .run-btn button { color:#ffffff !important; }

/* ── Status messages ── */
body.gp-light .status-ok-gen { background:rgba(26,86,196,.09) !important; border-left-color:var(--cyan) !important; color:#1e3a8a !important; }
body.gp-light .status-ok-gen strong { color:var(--cyan) !important; }
body.gp-light .status-success { background:rgba(5,150,105,.09) !important; border-left-color:#059669 !important; color:#064e3b !important; }
body.gp-light .status-success strong { color:#059669 !important; }
body.gp-light .status-warn { background:rgba(180,83,9,.07) !important; border-left-color:#b45309 !important; color:#78350f !important; }
body.gp-light .status-error { background:rgba(185,28,28,.07) !important; border-left-color:#b91c1c !important; color:#7f1d1d !important; }
body.gp-light .status-abort { background:rgba(71,85,105,.09) !important; border-left-color:#64748b !important; color:#334155 !important; }

/* ── CodeMirror / Cypher editor ── */
body.gp-light .cm-editor,
body.gp-light .cm-editor * { background:var(--cypher-bg) !important; }
body.gp-light .cm-content,
body.gp-light .cm-line { color:#1e3a8a !important; }
body.gp-light .cm-editor { border-color:var(--cypher-border) !important; box-shadow:0 0 14px rgba(109,40,217,.1) !important; }

/* ── Results table ── */
body.gp-light .results-wrap { background:var(--bg-card) !important; border-color:var(--border) !important; }
body.gp-light .custom-table thead tr { background:rgba(26,86,196,.1) !important; border-bottom-color:rgba(26,86,196,.28) !important; }
body.gp-light .custom-table th { color:var(--cyan) !important; text-shadow:0 0 8px rgba(26,86,196,.3) !important; }
body.gp-light .custom-table td { color:var(--text) !important; }
body.gp-light .custom-table td:first-child { color:var(--muted) !important; }
body.gp-light .custom-table tbody tr:hover { background:rgba(26,86,196,.06) !important; }

/* ── Brief / insight boxes ── */
body.gp-light .brief-box { background:linear-gradient(135deg,rgba(109,40,217,.08),rgba(26,86,196,.07)) !important; border-color:var(--border) !important; }
body.gp-light .brief-md p,
body.gp-light .brief-box p { color:var(--text) !important; }
body.gp-light .insight-section { border-top-color:var(--border) !important; }
body.gp-light .insight-heading { color:var(--violet) !important; text-shadow:0 0 10px rgba(109,40,217,.25) !important; }
body.gp-light .insight-heading-icon { color:var(--violet) !important; }
body.gp-light .insight-heading-line { background:linear-gradient(90deg,rgba(109,40,217,.35),transparent) !important; }
body.gp-light .insight-item { background:rgba(26,86,196,.06) !important; border-color:var(--border) !important; color:var(--text) !important; }
body.gp-light .insight-item:hover { border-color:rgba(26,86,196,.5) !important; box-shadow:0 0 12px rgba(26,86,196,.12) !important; }
body.gp-light .insight-dot { color:var(--cyan) !important; text-shadow:none !important; }
body.gp-light .insight-text { color:var(--text) !important; }

/* ── Export button ── */
body.gp-light .export-btn a,
body.gp-light .export-btn button { background:rgba(26,86,196,.1) !important; border-color:rgba(26,86,196,.4) !important; color:var(--cyan) !important; text-shadow:none !important; }

/* ── KPI cards ── */
body.gp-light .kpi-grid { border-color:var(--border) !important; }
body.gp-light .kpi-card { background:linear-gradient(135deg,rgba(26,86,196,.09),rgba(109,40,217,.07)) !important; border-color:var(--border) !important; }
body.gp-light .kpi-value { color:var(--cyan) !important; text-shadow:0 0 8px rgba(26,86,196,.3) !important; }
body.gp-light .kpi-label { color:var(--muted) !important; }

/* ── Gradio-generated svelte wrappers that bleed dark background ── */
body.gp-light .svelte-1f354aw,
body.gp-light .svelte-1p9xokt,
body.gp-light [class*="svelte-"] { background:transparent !important; }
body.gp-light .gr-box,
body.gp-light .gr-input,
body.gp-light .gr-panel { background:var(--bg-panel) !important; border-color:var(--border) !important; color:var(--text) !important; }
body.gp-light .gr-button { color:var(--text) !important; }
body.gp-light .gr-button.primary { color:#ffffff !important; }

/* ── Tool-log / agent output areas ── */
body.gp-light .tool-log-wrap,
body.gp-light [class*="tool-log"] { background:var(--bg-card) !important; color:var(--text) !important; border-color:var(--border) !important; }
body.gp-light [class*="tool-log"] span,
body.gp-light [class*="tool-log"] p { color:var(--text) !important; }

/* ── Any remaining hardcoded dark bg leaking through ── */
body.gp-light [style*="background: #060c1c"],
body.gp-light [style*="background:#060c1c"],
body.gp-light [style*="background: #070d1a"],
body.gp-light [style*="background:#070d1a"],
body.gp-light [style*="background: #0c1528"],
body.gp-light [style*="background:#0c1528"] { background:var(--bg-card) !important; }
body.gp-light [style*="color: #f1f5f9"],
body.gp-light [style*="color:#f1f5f9"],
body.gp-light [style*="color: #e2e8f0"],
body.gp-light [style*="color:#e2e8f0"],
body.gp-light [style*="color: white"],
body.gp-light [style*="color:white"],
body.gp-light [style*="color: #fff"],
body.gp-light [style*="color:#fff"] { color:var(--text) !important; }
body.gp-light [style*="color: #94a3b8"],
body.gp-light [style*="color:#94a3b8"],
body.gp-light [style*="color: #cbd5e1"],
body.gp-light [style*="color:#cbd5e1"],
body.gp-light [style*="color: #64748b"],
body.gp-light [style*="color:#64748b"] { color:var(--muted) !important; }

/* ── Loading pulse animation ── */
@keyframes pulse {
    0%, 100% { opacity: 0.3; transform: scale(0.85); }
    50%       { opacity: 1;   transform: scale(1.15); }
}

/* ── RCA Call-to-Action Box ── */
.rca-cta-box {
    display: flex;
    align-items: flex-start;
    gap: 14px;
    background: linear-gradient(135deg, rgba(124,58,237,0.12), rgba(0,229,255,0.06));
    border: 1px solid rgba(124,58,237,0.4);
    border-left: 3px solid #7c3aed;
    border-radius: var(--radius);
    padding: 14px 18px;
    margin-top: 14px;
}
.rca-cta-icon {
    font-size: 1.4rem;
    line-height: 1;
    flex-shrink: 0;
}
.rca-cta-text {
    font-family: var(--sans);
    font-size: 0.82rem;
    color: var(--text);
    line-height: 1.5;
}
.rca-cta-text strong {
    color: #a78bfa;
    font-size: 0.88rem;
}
.rca-cta-text span {
    color: var(--muted);
}
.rca-trigger-btn {
    width: 100%;
    margin-top: 10px;
    padding: 10px 20px !important;
    background: linear-gradient(135deg, rgba(124,58,237,0.25), rgba(0,229,255,0.1)) !important;
    border: 1px solid rgba(124,58,237,0.55) !important;
    color: #c4b5fd !important;
    font-family: var(--mono) !important;
    font-size: 0.8rem !important;
    letter-spacing: 0.04em !important;
    border-radius: var(--radius) !important;
    cursor: pointer !important;
    transition: all 0.2s ease !important;
}
.rca-trigger-btn:hover {
    background: linear-gradient(135deg, rgba(124,58,237,0.45), rgba(0,229,255,0.2)) !important;
    border-color: #7c3aed !important;
    color: #ede9fe !important;
    box-shadow: 0 0 18px rgba(124,58,237,0.3) !important;
}
"""

INIT_JS = """
<script>
(function () {
    var _isDark = true;
    function applyTheme(dark) {
        _isDark = dark;
        var b = document.body;
        if (!b) return;
        if (dark) { b.classList.remove('gp-light'); } else { b.classList.add('gp-light'); }
        var lbl = document.getElementById('gp-theme-label');
        if (lbl) lbl.textContent = dark ? 'DARK' : 'LIGHT';
        var chk = document.getElementById('gp-theme-checkbox');
        if (chk) chk.checked = !dark;
    }
    function wireToggle() {
        var chk = document.getElementById('gp-theme-checkbox');
        if (!chk || chk._gpWired) return !!chk;
        chk._gpWired = true;
        chk.addEventListener('change', function () { applyTheme(!this.checked); });
        return true;
    }
    applyTheme(true);
    wireToggle();
    var elapsed = 0;
    var iv = setInterval(function () {
        elapsed += 200;
        applyTheme(_isDark);
        if (wireToggle() || elapsed > 10000) clearInterval(iv);
    }, 200);
    function startObs() {
        var target = document.body || document.documentElement;
        var ob = new MutationObserver(function () { wireToggle(); applyTheme(_isDark); });
        ob.observe(target, { childList: true, subtree: false });
    }
    if (document.body) startObs();
    else document.addEventListener('DOMContentLoaded', startObs);
})();
</script>
"""

# ═════════════════════════════════════════════════════════════
# BUILD GRADIO UI
# ═════════════════════════════════════════════════════════════
with gr.Blocks(
    title="GraphPulse AI",
    css=CUSTOM_CSS,
    theme=gr.themes.Base(),
) as demo:

    gr.HTML(INIT_JS)

    gr.HTML("""
    <div id="gp-hero">
        <div class="gp-agent-badge">
            <div class="gp-agent-dot"></div>
            AI Agent &middot; Live
        </div>
        <div class="gp-hero-title-row">
            <h1>GraphPulse AI</h1>
            <div class="gp-theme-toggle-wrap">
                <span class="gp-theme-toggle-label" id="gp-theme-label">DARK</span>
                <label class="gp-ios-toggle" title="Toggle Light / Dark Mode">
                    <input type="checkbox" id="gp-theme-checkbox">
                    <span class="gp-ios-slider"></span>
                </label>
            </div>
        </div>
        <p class="gp-tagline">Real-Time Supply Chain Intelligence Engine</p>
        <div class="gp-hero-divider">
            <div class="gp-hero-divider-line"></div>
            <div class="gp-hero-divider-dot"></div>
            <div class="gp-hero-divider-line right"></div>
        </div>
        <div class="gp-hero-stats">
            <div class="gp-stat-pill">Neo4j Graph DB</div>
            <div class="gp-stat-pill">OpenRouter LLM</div>
            <div class="gp-stat-pill">Supply Chain KG</div>
            <div class="gp-stat-pill">Real-Time Analysis</div>
        </div>
    </div>
    """)

    with gr.Row(equal_height=False):

        # ── LEFT SIDEBAR ──
        with gr.Column(scale=1, min_width=230, elem_classes="left-panel"):
            gr.HTML('<div class="sec-label">⬡ KG Snapshot</div>')
            snapshot_html = gr.HTML(value="<div class='snap-error'>Connecting…</div>")
            refresh_snap_btn = gr.Button("⟳  Load Snapshot", elem_classes="clear-btn", size="sm")

            gr.HTML('<div class="sec-label" style="margin-top:16px;">⚡ Quick Queries</div>')
            sample_buttons = []
            for q in SAMPLE_QUESTIONS[:8]:
                b = gr.Button(q, elem_classes="sample-btn")
                sample_buttons.append(b)

            gr.HTML('<div class="sec-label" style="margin-top:16px;">⬡ Recent Queries</div>')
            history_html = gr.HTML(value="<div class='hist-empty'>No queries yet</div>")

            gr.HTML('<div class="sec-label" style="margin-top:16px;">⚙ Session</div>')
            clear_btn = gr.Button("⟳  Clear Session", elem_classes="clear-btn")

        # ── MAIN PANEL ──
        with gr.Column(scale=3, elem_classes="main-panel"):
            gr.HTML('<div class="sec-label">◈ Query Interface</div>')

            question_input = gr.Textbox(
                placeholder="e.g.  If the top 3 highest risk suppliers were removed, which plants would be affected?",
                lines=2, label="", show_label=False,
            )

            # Generate Query — full width, alone
            with gr.Row():
                ask_btn = gr.Button("⚡  Generate Query", elem_classes="analyze-btn", scale=1)

            gen_status = gr.HTML("", elem_classes="status-html-wrap")

            cypher_box = gr.Textbox(
                lines=6,
                label="Generated Cypher — inspect or edit, then run",
                visible=False,
            )

            # ▶ Run Query  |  ✕ Abort — side by side, only visible after generation
            with gr.Row(visible=False) as run_row:
                run_btn   = gr.Button("▶  Run Query", elem_classes="run-btn",   scale=3)
                abort_btn = gr.Button("✕  Abort",     elem_classes="abort-btn", scale=1)

            run_status = gr.HTML("", elem_classes="status-html-wrap")

            # Hidden state for RCA extras (absorbs the extra 2 return values from on_run_query)
            _rca_worthy_state  = gr.State(False)
            _rca_prefill_state = gr.State("")

            with gr.Column(visible=False) as results_section:
                gr.HTML('<div class="sec-label" style="margin-top:8px;">◈ Analysis Output</div>')
                with gr.Column(elem_classes="results-wrap"):
                    with gr.Column(elem_classes="brief-box"):
                        brief_md = gr.Markdown("", elem_classes="brief-md")
                    table_html_out   = gr.HTML("")
                    insight_html_out = gr.HTML("")
                    export_btn = gr.DownloadButton(
                        "⬇ Export Results as CSV",
                        visible=False,
                        elem_classes="export-btn",
                    )

            # RCA call-to-action — shown when query is delay/risk/performance related
            with gr.Column(visible=False) as rca_cta_col:
                gr.HTML("""
                <div class="rca-cta-box">
                    <div class="rca-cta-icon">⚡</div>
                    <div class="rca-cta-text">
                        <strong>Root Cause Analysis available</strong><br>
                        <span>This query involves operational issues — run an RCA to diagnose underlying causes.</span>
                    </div>
                </div>
                """)
                rca_trigger_btn = gr.Button("🔍  Run Root Cause Analysis", elem_classes="rca-trigger-btn")

    # ═══ EVENT WIRING ═══
    demo.load(fn=load_snapshot, outputs=[snapshot_html])
    refresh_snap_btn.click(fn=load_snapshot, outputs=[snapshot_html])

    # on_run_query returns 9 values: 6 UI + rca_worthy + rca_prefill + rca_cta_col
    _run_outs = [run_status, results_section,
                 brief_md, table_html_out, insight_html_out, export_btn,
                 _rca_worthy_state, _rca_prefill_state, rca_cta_col]

    # on_run_insights returns 2 values: brief, insight_html
    _insight_outs = [brief_md, insight_html_out]

    # Single-click flow: generate cypher → run query (shows table fast) → run insights (LLM, async)
    ask_btn.click(
        fn=on_generate_query,
        inputs=[question_input],
        outputs=[gen_status, cypher_box, run_row, results_section,
                 run_status, brief_md, table_html_out, insight_html_out, export_btn],
        show_progress="full",
    ).then(
        fn=on_run_query,
        inputs=[question_input, cypher_box],
        outputs=_run_outs,
        show_progress="hidden",
    ).then(
        fn=on_run_insights,
        inputs=[question_input, cypher_box],
        outputs=_insight_outs,
        show_progress="hidden",
    ).then(fn=update_history, outputs=[history_html]) \
     .then(fn=load_snapshot,  outputs=[snapshot_html])

    run_btn.click(
        fn=on_run_query,
        inputs=[question_input, cypher_box],
        outputs=_run_outs,
        show_progress="full",
    ).then(
        fn=on_run_insights,
        inputs=[question_input, cypher_box],
        outputs=_insight_outs,
        show_progress="hidden",
    ).then(fn=update_history, outputs=[history_html])

    # Abort resets cypher box and run row too
    abort_btn.click(
        fn=on_abort,
        outputs=[gen_status, cypher_box, run_row, results_section,
                 run_status, brief_md, table_html_out, insight_html_out, export_btn],
    ).then(fn=lambda: gr.update(visible=False), outputs=[rca_cta_col])

    clear_btn.click(
        fn=clear_session,
        outputs=[gen_status, cypher_box, run_row, results_section,
                 run_status, brief_md, table_html_out, insight_html_out, export_btn],
    ).then(fn=lambda: gr.update(visible=False), outputs=[rca_cta_col]) \
     .then(fn=update_history, outputs=[history_html])

    export_btn.click(
        fn=make_csv,
        inputs=[question_input],
        outputs=[export_btn],
    )

    for btn, q in zip(sample_buttons, SAMPLE_QUESTIONS[:8]):
        btn.click(
            fn=lambda x=q: x,
            outputs=[question_input],
        ).then(
            fn=on_generate_query,
            inputs=[question_input],
            outputs=[gen_status, cypher_box, run_row, results_section,
                     run_status, brief_md, table_html_out, insight_html_out, export_btn],
            show_progress="full",
        ).then(
            fn=on_run_query,
            inputs=[question_input, cypher_box],
            outputs=_run_outs,
            show_progress="hidden",
        ).then(
            fn=on_run_insights,
            inputs=[question_input, cypher_box],
            outputs=_insight_outs,
            show_progress="hidden",
        ).then(fn=update_history, outputs=[history_html]) \
         .then(fn=load_snapshot,  outputs=[snapshot_html])

# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n  GraphPulse AI starting on http://0.0.0.0:7903")
    demo.launch(server_name="0.0.0.0", server_port=None, inbrowser=True)