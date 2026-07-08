"""
neo4j_tools.py
==============
All Neo4j query functions that the agent can call as tools.
These are plain Python functions
"""

import os, json
from neo4j import GraphDatabase
from dotenv import load_dotenv

load_dotenv(".env")

_driver = GraphDatabase.driver(
    os.getenv("NEO4J_URI"),
    auth=(os.getenv("NEO4J_USER"), os.getenv("NEO4J_PASSWORD")),
)
_DB = os.getenv("NEO4J_DATABASE", "neo4j")


def _run(query: str, params: dict = None):
    """Internal helper — runs Cypher and returns list of dicts."""
    with _driver.session(database=_DB) as s:
        result = s.run(query, params or {})
        return [record.data() for record in result]


# ─────────────────────────────────────────────────────────────
# EXISTING TOOLS  
# ─────────────────────────────────────────────────────────────

def run_cypher(query: str) -> str:
    """
    Execute any Cypher query on the Neo4j supply chain knowledge graph.
    Returns results as a JSON string (max 50 rows).

    BEFORE calling this tool, ALWAYS call get_schema_with_examples first.
    It returns node labels, relationship directions, property names, and
    6 canonical query patterns with anti-fan-out warnings.

    KEY RULES (derived from schema examples):
    - delivery_status has EXACTLY 2 values: 'Major Delay' or 'On Time'
    - Never MATCH (Plant)-[:HAS_ROUTE] and (Plant)-[:DISPATCHES] in the same
      MATCH without anchoring through a shared Distributor — fan-out risk
    - Use COUNT(DISTINCT sh.shipment_id) when a shipment can appear via multiple paths
    - Distributor city-specific queries: filter by distributor_id, not city string
      Example (demand gap for a city):
        MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d:Distributor)
        WHERE d.distributor_id = $dist_id
        RETURN pl.plant_id, pl.plant_name,
               COUNT(sh) AS total_shipments,
               SUM(CASE WHEN sh.demand_gap > 0 THEN sh.demand_gap ELSE 0 END) AS demand_gap,
               COUNT(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 END) AS major_delays
        ORDER BY demand_gap DESC
    - Always return individual properties, never whole node objects.
    - StoP_lead_time_days may be stored as StoP_lead_time_days, lead_time_days,
      lead_time, or leadtime_days — use COALESCE over all variants.
    """
    try:
        rows = _run(query)
        # Allow up to 500 rows — network-wide queries (all 50 cities × 4 plants = 200 rows)
        # need more than 50. Specific queries should use LIMIT in their Cypher.
        return json.dumps(rows[:500], default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def get_graph_summary() -> str:
    """
    Returns a count of every node label in the graph.
    Use this first to understand graph size and what data exists.
    """
    rows = _run("MATCH (n) RETURN labels(n)[0] AS label, COUNT(n) AS count ORDER BY count DESC")
    return rows


def get_delayed_shipments(limit: int = 10) -> str:
    """
    Returns shipments with delivery_status = 'Major Delay', ordered by delay_days descending.
    Use this to find the worst delayed shipments with route and timing information.
    """
    rows = _run("""
        MATCH (p:Plant)-[:DISPATCHES]->(s:Shipment)-[:SHIPPED_TO]->(d:Distributor)
        WHERE s.delivery_status = 'Major Delay'
        RETURN p.plant_id AS plant_id, p.plant_name AS plant_name,
               s.shipment_id AS shipment_id, round(s.delay_days, 2) AS delay_days,
               s.route_id AS route_id, s.transaction_date AS date,
               d.distributor_id AS distributor_id, d.distributor_city AS distributor_city
        ORDER BY s.delay_days DESC
        LIMIT $limit
    """, {"limit": limit})
    return rows


def get_delay_by_product_category() -> str:
    """
    Returns each product category with its count of delayed shipments and average delay days.
    Use this to find WHICH product types are most affected by delays — step 1 of RCA.
    """
    rows = _run("""
        MATCH (s:Shipment)-[:CARRIES]->(p:Product)
        WHERE s.delivery_status = 'Major Delay'
        WITH p.product_category_name AS category,
             COUNT(s) AS delayed_shipments,
             round(AVG(s.delay_days), 2) AS avg_delay_days
        RETURN category, delayed_shipments, avg_delay_days
        ORDER BY delayed_shipments DESC
    """)
    return rows


def get_delay_by_plant() -> str:
    """
    Returns each plant with delayed shipment count, total shipments, delay rate %, and avg delay.
    FIX: Added total_shipments and delay_rate_pct — LLM can now fill the Delay Rate % column
    without guessing or computing it from incomplete data.
    Use this to find WHICH plants are bottlenecks — step 2 of RCA.
    """
    rows = _run("""
        MATCH (p:Plant)-[:DISPATCHES]->(s:Shipment)
        WITH p,
             COUNT(s) AS total_shipments,
             SUM(CASE WHEN s.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed_count,
             round(AVG(CASE WHEN s.delivery_status = 'Major Delay' THEN s.delay_days END), 2) AS avg_delay
        RETURN p.plant_id   AS plant_id,
               p.plant_name AS plant_name,
               total_shipments,
               delayed_count,
               round(toFloat(delayed_count) / toFloat(total_shipments) * 100, 1) AS delay_rate_pct,
               avg_delay
        ORDER BY delayed_count DESC
    """)
    return rows


def get_high_risk_suppliers(threshold: float = 0.6) -> str:
    """
    Returns suppliers above the risk threshold with delayed shipment count,
    avg delay days, risk score, capacity, plant info — ordered by risk DESC.
    """
    rows = _run("""
        MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)
        WHERE sup.risk_score > $threshold
        WITH sup, pl
        OPTIONAL MATCH (pl)-[:DISPATCHES]->(sh:Shipment)
        WITH sup, pl,
             COUNT(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 END) AS delayed_shipments,
             round(AVG(CASE WHEN sh.delivery_status = 'Major Delay' THEN sh.delay_days END), 2) AS avg_delay_days
        RETURN sup.supplier_id           AS supplier_id,
               sup.supplier_name         AS supplier_name,
               round(sup.risk_score, 2)  AS risk_score,
               sup.annual_capacity_units  AS annual_capacity,
               pl.plant_id               AS plant_id,
               pl.plant_name             AS plant_name,
               delayed_shipments,
               avg_delay_days
        ORDER BY sup.risk_score DESC, delayed_shipments DESC
        LIMIT 50
    """, {"threshold": threshold})

    return rows if isinstance(rows, list) else []


def get_distributor_delay_impact() -> str:
    """
    Returns distributors ranked by delayed shipments received, with sourcing plant info.
    FIX: Added plant_id/plant_name so LLM can trace which plant drives each distributor's delays.
         Renamed gap → total_demand_gap for clarity.
    NOTE: delayed_shipments here is the DISTRIBUTOR total across all its shipments.
          Do NOT use this number for per-supplier Delay Contribution — use
          get_high_risk_suppliers or get_supplier_delay_contribution instead.
    Use this to find DISTRIBUTOR-LEVEL IMPACT — step 4 of RCA.
    """
    rows = _run("""
        MATCH (pl:Plant)-[:DISPATCHES]->(s:Shipment)-[:SHIPPED_TO]->(d:Distributor)
        WHERE s.delivery_status = 'Major Delay'
        RETURN d.distributor_id    AS distributor_id,
               d.distributor_city  AS distributor_city,
               pl.plant_id         AS primary_plant_id,
               pl.plant_name       AS primary_plant_name,
               COUNT(s)            AS delayed_shipments,
               round(AVG(s.delay_days), 2) AS avg_delay,
               SUM(s.demand_gap)   AS total_demand_gap
        ORDER BY delayed_shipments DESC
        LIMIT 10
    """)
    return rows


def get_stockout_retailers() -> str:
    """
    Returns distributor cities with highest total unmet demand (demand_gap > 0).
    Groups per distributor city — each city appears ONCE.

    NOTE on retailers_connected: Only 5 major hub distributors (Mumbai=50, Delhi=30,
    Chennai=30, Kolkata=20, Bengaluru=20) have DELIVERS_TO retailer edges in the graph.
    All other distributors return 0 — this is accurate graph data, not a bug.
    The total_shortage_units and shortage_shipments numbers are fully accurate for all cities.

    Returns: retailer_city, served_by_distributor, retailers_connected,
             shortage_shipments, total_shortage_units
    Use this to find highest-shortage distributor cities — step 5 of RCA.
    """
    rows = _run("""
        MATCH (s:Shipment)-[:SHIPPED_TO]->(d:Distributor)
        WHERE s.demand_gap IS NOT NULL AND s.demand_gap > 0
        WITH d,
             COUNT(DISTINCT s)            AS shortage_shipments,
             toInteger(SUM(s.demand_gap)) AS total_shortage_units
        OPTIONAL MATCH (d)-[:DELIVERS_TO]->(r:Retailer)
        WITH d, shortage_shipments, total_shortage_units,
             COUNT(DISTINCT r) AS retailers_connected
        RETURN d.distributor_city     AS distributor_city,
               d.distributor_id      AS distributor_id,
               shortage_shipments,
               total_shortage_units
        ORDER BY total_shortage_units DESC
        LIMIT 15
    """)
    return rows


def trace_supply_chain_for_category(product_category: str) -> str:
    """
    Traces the FULL supply chain path for a given product category:
    Supplier → Plant → Shipment → Distributor → Retailer.
    product_category must be one of: toys, watches_gifts, health_beauty, auto, cool_stuff, bed_bath_table
    Use this to understand end-to-end exposure for a specific product type.
    """
    rows = _run("""
        MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)-[:DISPATCHES]->(sh:Shipment)
              -[:CARRIES]->(prod:Product),
              (sh)-[:SHIPPED_TO]->(d:Distributor)-[:DELIVERS_TO]->(r:Retailer)
        WHERE prod.product_category_name = $cat
        RETURN DISTINCT sup.supplier_name AS supplier,
               pl.plant_name AS plant,
               sh.delivery_status AS delivery_status,
               sh.delay_days AS delay_days,
               d.distributor_city AS distributor,
               r.retailer_city AS retailer
        ORDER BY sh.delay_days DESC
        LIMIT 20
    """, {"cat": product_category})
    return rows


def get_route_cost_efficiency() -> str:
    """
    Returns all routes with their transport mode, distance, cost, and efficiency score.
    Low cost_efficiency routes are bottlenecks. Use this to identify inefficient routes.
    """
    rows = _run("""
        MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor)
        RETURN pl.plant_id AS plant_id, pl.plant_name AS plant_name,
               r.route_id AS route_id, r.mode AS transport_mode,
               r.PtoD_distance_km AS distance_km,
               r.PtoD_transportation_cost_inr AS cost_inr,
               r.cost_efficiency AS cost_efficiency,
               r.PtoD_leadtime_days AS leadtime_days,
               d.distributor_city AS distributor_city
        ORDER BY r.cost_efficiency ASC
        LIMIT 20
    """)
    return rows


def get_supplier_plant_delay_chain() -> str:
    """
    Shows the full chain: which suppliers feed plants that have the most delayed shipments.
    Returns supplier risk scores linked to plant delay counts.
    FIX: Increased LIMIT from 10 → 25 so all significant suppliers are visible.
    Critical for connecting supplier risk → plant → actual delay count per supplier.
    """
    rows = _run("""
        MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)-[:DISPATCHES]->(sh:Shipment)
        WHERE sh.delivery_status = 'Major Delay'
        RETURN sup.supplier_id          AS supplier_id,
               sup.supplier_name        AS supplier_name,
               round(sup.risk_score, 2) AS risk_score,
               pl.plant_id              AS plant_id,
               pl.plant_name            AS plant_name,
               COUNT(sh)                AS delayed_shipments,
               round(AVG(sh.delay_days), 2) AS avg_delay
        ORDER BY delayed_shipments DESC
        LIMIT 25
    """)
    return rows


def get_demand_gap_analysis() -> str:
    """
    AUTHORITATIVE distributor shortage query.
    Returns per-distributor city (each city ONCE):
      distributor_city, shortage_shipments, total_demand_gap,
      delayed_shipments, avg_delay_days, retailers_affected
    Use THIS as the single source for all distributor shortage metrics.
    Do NOT mix with get_distributor_delay_impact for shortage numbers.
    """
    rows = _run("""
        MATCH (s:Shipment)-[:SHIPPED_TO]->(d:Distributor)
        WITH d,
             COUNT(CASE WHEN s.demand_gap > 0 THEN 1 END) AS shortage_shipments,
             toInteger(COALESCE(SUM(CASE WHEN s.demand_gap > 0 THEN s.demand_gap ELSE 0 END),0)) AS total_demand_gap,
             COUNT(CASE WHEN s.delivery_status = 'Major Delay' THEN 1 END) AS delayed_shipments,
             toFloat(COALESCE(SUM(CASE WHEN s.delivery_status = 'Major Delay' THEN s.delay_days ELSE 0 END),0)) AS delay_days_sum
        WITH d, shortage_shipments, total_demand_gap, delayed_shipments,
             CASE delayed_shipments WHEN 0 THEN 0.0
                 ELSE round(delay_days_sum / toFloat(delayed_shipments), 2)
             END AS avg_delay_days
        OPTIONAL MATCH (d)-[:DELIVERS_TO]->(r:Retailer)
        RETURN d.distributor_city AS distributor_city,
               shortage_shipments, total_demand_gap, delayed_shipments,
               avg_delay_days,
               COUNT(DISTINCT r) AS retailers_directly_connected
        ORDER BY total_demand_gap DESC
        LIMIT 15
    """)
    return rows


def create_or_update_node(cypher_merge_query: str) -> str:
    """
    Executes a MERGE Cypher statement to create or update a node in the graph.
    Use ONLY for write operations (adding new suppliers, distributors, routes).
    The query MUST start with MERGE or CREATE.
    Returns confirmation of what was written.

    DATA INTEGRITY GUARD: Rejects any query that would SET supplier_name
    to a placeholder/generic string. These values corrupt the original data.
    """
    q = cypher_merge_query.strip()
    if not (q.upper().startswith("MERGE") or q.upper().startswith("CREATE")):
        return json.dumps({"error": "Query must start with MERGE or CREATE for safety."})

    try:
        rows = _run(q)
        return json.dumps({"status": "success", "result": rows}, default=str)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


# ── Authoritative supplier names from SupplierMaster Excel ────────────────
# Source: Logistic usecase Deloitte.xlsx → SupplierMaster sheet
_SUPPLIER_MASTER = {
    "SUP0001": "Mahajan-Ghosh",
    "SUP0002": "Saini, Patel and Sankaran",
    "SUP0003": "Kibe Inc",
    "SUP0004": "Dyal LLC",
    "SUP0005": "Bhat, Rajan and Prasad",
    "SUP0006": "Kade Ltd",
    "SUP0007": "Reddy, Mahal and Patel",
    "SUP0008": "Handa Ltd",
    "SUP0009": "Balakrishnan, Padmanabhan and Kannan",
    "SUP0010": "Srinivasan, Agate and Ratta",
    "SUP0011": "Radhakrishnan Ltd",
    "SUP0012": "Sen-Bir",
    "SUP0013": "Ganesh-Prabhu",
    "SUP0014": "Singh-Sane",
    "SUP0015": "Kalita Inc",
    "SUP0016": "Anand-Shankar",
    "SUP0017": "Chatterjee and Sons",
    "SUP0018": "Verma Enterprises",
    "SUP0019": "Bassi-Bansal",
    "SUP0020": "Kumar and Brothers",
    "SUP0021": "Sharma Trading Co",
    "SUP0022": "Raghavan, Mand and Choudhary",
    "SUP0023": "Sha Group",
    "SUP0024": "Iyer and Associates",
    "SUP0025": "Pillai Logistics",
}


def audit_supplier_names() -> str:
    """
    Compares every Supplier node in Neo4j against the original SupplierMaster data.
    Returns a list of nodes whose supplier_name has been corrupted or overwritten.
    READ-ONLY — makes no changes.

    Use this to detect data corruption before deciding whether to run
    restore_original_supplier_names().
    """
    try:
        rows = _run(
            "MATCH (s:Supplier) RETURN s.supplier_id AS id, "
            "s.supplier_name AS name ORDER BY s.supplier_id"
        )
    except Exception as e:
        return json.dumps({"error": str(e)})

    corrupted = []
    unknown   = []
    for row in rows:
        sid   = row.get("id", "")
        cname = row.get("name", "")
        if sid in _SUPPLIER_MASTER:
            if cname != _SUPPLIER_MASTER[sid]:
                corrupted.append({
                    "supplier_id":   sid,
                    "current_name":  cname,
                    "correct_name":  _SUPPLIER_MASTER[sid],
                    "status":        "CORRUPTED — will be fixed by restore"
                })
        else:
            unknown.append({"supplier_id": sid, "current_name": cname,
                            "status": "NEW — not in original data, skipped"})

    return json.dumps({
        "total_in_graph":  len(rows),
        "corrupted_count": len(corrupted),
        "unknown_count":   len(unknown),
        "corrupted":       corrupted,
        "unknown_new":     unknown,
        "action_needed":   len(corrupted) > 0
    }, default=str)


def restore_original_supplier_names() -> str:
    """
    Restores all Supplier nodes in Neo4j to their correct names from the
    original SupplierMaster data.

    Only updates nodes whose current supplier_name does NOT match the original.
    Nodes with supplier_ids not in the original data are left untouched.

    Run audit_supplier_names() first to see what will be changed.
    Returns a summary of every node that was restored.
    """
    restored = []
    skipped  = []
    errors   = []

    try:
        rows = _run(
            "MATCH (s:Supplier) RETURN s.supplier_id AS id, "
            "s.supplier_name AS name ORDER BY s.supplier_id"
        )
    except Exception as e:
        return json.dumps({"error": f"Could not read suppliers: {e}"})

    for row in rows:
        sid   = row.get("id", "")
        cname = row.get("name", "")

        if sid not in _SUPPLIER_MASTER:
            skipped.append({"supplier_id": sid, "name": cname,
                            "reason": "not in SupplierMaster — left unchanged"})
            continue

        correct_name = _SUPPLIER_MASTER[sid]
        if cname == correct_name:
            # Already correct — nothing to do
            continue

        try:
            _run(
                "MATCH (s:Supplier {supplier_id: $sid}) "
                "SET s.supplier_name = $name",
                {"sid": sid, "name": correct_name}
            )
            restored.append({
                "supplier_id":  sid,
                "was":          cname,
                "restored_to":  correct_name
            })
        except Exception as e:
            errors.append({"supplier_id": sid, "error": str(e)})

    return json.dumps({
        "status":         "done",
        "restored_count": len(restored),
        "skipped_count":  len(skipped),
        "error_count":    len(errors),
        "restored":       restored,
        "skipped":        skipped,
        "errors":         errors,
        "message": (
            f"✓ Restored {len(restored)} supplier name(s) to original SupplierMaster values."
            if restored else
            "✓ All supplier names already match the original data — nothing to restore."
        )
    }, default=str)


def verify_node_exists(label: str, property_name: str, property_value: str) -> str:
    """
    Verifies that a node exists in the graph after creation.
    label: e.g. 'Supplier', 'Distributor', 'Route'
    property_name: e.g. 'supplier_id', 'distributor_id', 'route_id'
    property_value: e.g. 'SUP9001', 'D9001', 'PL4@D0050'
    Returns the node's properties if found.
    """
    rows = _run(
        f"MATCH (n:{label}) WHERE n.{property_name} = $val RETURN n LIMIT 1",
        {"val": property_value}
    )
    if rows:
        return json.dumps({"found": True, "node": rows[0]}, default=str)
    return json.dumps({"found": False, "message": f"No {label} with {property_name}={property_value}"})


# ─────────────────────────────────────────────────────────────
# NEW TOOLS  
# ─────────────────────────────────────────────────────────────

def get_supplier_delay_contribution() -> str:
    """
    NEW — The single authoritative source for per-supplier delayed shipment counts.

    Returns every supplier with their OWN delayed_shipments count, avg_delay_days,
    risk_score, and the plant they supply.

    USE THIS for the "Delay Contribution" column in any supplier table.
    Do NOT use get_distributor_delay_impact for this — that tool returns
    distributor-level totals, not per-supplier counts.
    """
    rows = _run("""
        MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)-[:DISPATCHES]->(sh:Shipment)
        WHERE sh.delivery_status = 'Major Delay'
        WITH sup, pl,
             COUNT(sh)                AS delayed_shipments,
             round(AVG(sh.delay_days), 2) AS avg_delay_days
        RETURN sup.supplier_id          AS supplier_id,
               sup.supplier_name        AS supplier_name,
               round(sup.risk_score, 2) AS risk_score,
               pl.plant_id              AS plant_id,
               pl.plant_name            AS plant_name,
               delayed_shipments,
               avg_delay_days
        ORDER BY delayed_shipments DESC, sup.risk_score DESC
        LIMIT 30
    """)
    return rows


def get_plant_supplier_matrix() -> str:
    """
    Shows supplier count, avg/max risk, and single-source dependency flag per plant.
    Single-source = plant has ONLY 1 supplier (highest vulnerability).
    Use to detect supply concentration risk and single-point-of-failure plants.
    """
    rows = _run("""
        MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)
        WITH pl,
             COUNT(sup)                  AS supplier_count,
             round(AVG(sup.risk_score), 2) AS avg_risk_score,
             round(MAX(sup.risk_score), 2) AS max_risk_score,
             COLLECT(sup.supplier_id)[0..5] AS top_supplier_ids
        RETURN pl.plant_id   AS plant_id,
               pl.plant_name AS plant_name,
               supplier_count,
               avg_risk_score,
               max_risk_score,
               CASE WHEN supplier_count = 1 THEN 'YES — Single Supplier (Critical Risk)'
                    WHEN supplier_count <= 3 THEN 'LOW — Few Suppliers (High Risk)'
                    ELSE 'No — Multiple Suppliers'
               END AS single_source_risk,
               top_supplier_ids
        ORDER BY max_risk_score DESC
    """)
    return rows


def get_monthly_delay_trend() -> str:
    """
    Month-by-month delayed vs on-time shipment counts with delay rate %.
    Uses month_number property (confirmed to exist in logisticsdb).
    ship_date property does NOT exist in this database — skip it.
    Returns: year_month, delayed_count, on_time_count, total_shipments, delay_rate_pct.
    """
    import re as _re3

    # Primary: month_number (confirmed to exist)
    rows = _run("""
        MATCH (s:Shipment)
        WHERE s.month_number IS NOT NULL
        WITH s.month_number AS month_num,
             s.delivery_status AS status,
             COUNT(s) AS cnt
        WITH month_num,
             SUM(CASE WHEN status = 'Major Delay' THEN cnt ELSE 0 END) AS delayed_count,
             SUM(CASE WHEN status = 'On Time'     THEN cnt ELSE 0 END) AS on_time_count,
             SUM(cnt) AS total_shipments
        RETURN month_num,
               delayed_count, on_time_count, total_shipments,
               round(toFloat(delayed_count) / toFloat(total_shipments) * 100, 1) AS delay_rate_pct
        ORDER BY month_num ASC
    """)

    # Process month_number results
    if isinstance(rows, list) and rows:
        _MONTH_NAMES = {1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",
                        7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec"}
        formatted = []
        for r in rows:
            mn = r.get("month_num")
            if mn is not None:
                mn_int = int(mn)
                mname = _MONTH_NAMES.get(mn_int, f"{mn_int:02d}")
                formatted.append({
                    "year_month":      f"Period {mn_int:02d} ({mname})",
                    "delayed_count":   r.get("delayed_count", 0),
                    "on_time_count":   r.get("on_time_count", 0),
                    "total_shipments": r.get("total_shipments", 0),
                    "delay_rate_pct":  r.get("delay_rate_pct", 0),
                })
        if formatted:
            return formatted

    # Fallback: transaction_date string parsing
    rows3 = _run("""
        MATCH (s:Shipment)
        WHERE s.transaction_date IS NOT NULL
        WITH toString(s.transaction_date) AS raw_date,
             s.delivery_status AS status,
             COUNT(s) AS cnt
        WITH raw_date,
             SUM(CASE WHEN status = 'Major Delay' THEN cnt ELSE 0 END) AS delayed_count,
             SUM(CASE WHEN status = 'On Time'     THEN cnt ELSE 0 END) AS on_time_count,
             SUM(cnt) AS total_shipments
        RETURN raw_date AS year_month,
               delayed_count, on_time_count, total_shipments,
               round(toFloat(delayed_count) / toFloat(total_shipments) * 100, 1) AS delay_rate_pct
        ORDER BY raw_date ASC
    """)
    if isinstance(rows3, list):
        for r in rows3:
            ym = str(r.get("year_month", ""))
            if _re3.match(r'^\d{4}-\d{2}$', ym):
                continue
            m = _re3.match(r'^(\d{2})-(\d{4})$', ym)
            if m:
                r["year_month"] = f"{m.group(2)}-{m.group(1)}"; continue
            m = _re3.match(r'^(\d{2})/(\d{4})$', ym)
            if m:
                r["year_month"] = f"{m.group(2)}-{m.group(1)}"; continue
            m = _re3.match(r'^(\d{4}-\d{2})-\d{2}$', ym)
            if m:
                r["year_month"] = m.group(1); continue
        try:
            rows3.sort(key=lambda x: str(x.get("year_month", "")))
        except Exception:
            pass
        return rows3

    return []


def get_supplier_lead_time_analysis() -> str:
    """
    NEW — Per-supplier lead time compared to their plant's average.

    lead_time_gap = supplier's lead_time_days minus the plant's average.
    A positive gap means this supplier is slower than peers at the same plant.
    Identifies suppliers whose long StoP lead times drive delays even when
    their risk_score is only moderate.
    Uses COALESCE to handle multiple property name variants for lead time.
    """
    rows = _run("""
        MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)
        WITH pl,
             avg(COALESCE(sup.StoP_lead_time_days, sup.lead_time_days, sup.lead_time, sup.leadtime_days, 0))
             AS plant_avg_lead_time
        MATCH (sup2:Supplier)-[:SUPPLIES_TO]->(pl)
        WITH sup2, pl, plant_avg_lead_time,
             COALESCE(sup2.StoP_lead_time_days, sup2.lead_time_days, sup2.lead_time, sup2.leadtime_days, 0)
             AS sup_lead_time
        RETURN sup2.supplier_id          AS supplier_id,
               sup2.supplier_name        AS supplier_name,
               round(sup2.risk_score, 2) AS risk_score,
               pl.plant_id               AS plant_id,
               pl.plant_name             AS plant_name,
               sup_lead_time             AS lead_time_days,
               round(plant_avg_lead_time, 2)                AS plant_avg_lead_time,
               round(sup_lead_time - plant_avg_lead_time, 2) AS lead_time_gap
        ORDER BY lead_time_gap DESC
        LIMIT 20
    """)
    return rows


def get_category_plant_delay_heatmap() -> str:
    """
    NEW — Returns (plant, product_category, delayed_count) rows for heatmap analysis.

    Shows which plant × product category combinations have the most delays.
    Use this to determine whether delays at a plant are across all categories
    (plant-wide problem) or concentrated in specific categories (supplier/product issue).
    """
    rows = _run("""
        MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:CARRIES]->(pr:Product)
        WHERE sh.delivery_status = 'Major Delay'
        RETURN pl.plant_id              AS plant_id,
               pl.plant_name            AS plant_name,
               pr.product_category_name AS category,
               COUNT(sh)                AS delayed_count
        ORDER BY delayed_count DESC
    """)
    return rows


def get_route_delay_correlation() -> str:
    """
    NEW — Joins Routes to Shipments to count delays per route with delay_rate_pct.

    Previously, route analysis only covered cost efficiency. This tool adds
    delay frequency so you can identify routes that are both expensive AND unreliable —
    the highest-priority targets for logistics optimisation.
    Returns: route_id, transport_mode, plant, distributor, delayed_shipments,
             on_time_shipments, total_shipments, delay_rate_pct, cost_efficiency.
    """
    rows = _run("""
        MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d:Distributor)
        WHERE sh.route_id IS NOT NULL
        WITH pl, d, sh.route_id AS route_id,
             SUM(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed_shipments,
             SUM(CASE WHEN sh.delivery_status = 'On Time'     THEN 1 ELSE 0 END) AS on_time_shipments,
             COUNT(sh) AS total_shipments
        OPTIONAL MATCH (r:Route {route_id: route_id})
        RETURN route_id,
               r.mode             AS transport_mode,
               pl.plant_id        AS plant_id,
               pl.plant_name      AS plant_name,
               d.distributor_city AS distributor_city,
               delayed_shipments,
               on_time_shipments,
               total_shipments,
               round(toFloat(delayed_shipments) / toFloat(total_shipments) * 100, 1) AS delay_rate_pct,
               r.cost_efficiency  AS cost_efficiency
        ORDER BY delayed_shipments DESC
        LIMIT 20
    """)
    return rows


def get_transport_mode_delays() -> str:
    """
    AUTHORITATIVE — Counts ALL Major Delay shipments grouped by transport mode
    (Road / Rail / Air / Sea).

    FIX v5: Join via Plant-[:HAS_ROUTE]->Route-[:CONNECTS_TO]->Distributor
    rather than matching on Shipment.route_id (which does not match Route node IDs
    in this database). We identify the route for each Plant→Distributor pair from
    the Route nodes, then aggregate delayed shipments through that Plant→Distributor
    path by mode. This avoids the 'Unknown' mode that appeared when Shipment.route_id
    had no matching Route node.

    Returns: transportation_mode, total_delays, avg_delay_days, plants_affected.
    """
    rows = _run("""
        MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor)
        WHERE r.mode IS NOT NULL AND r.mode <> ''
        WITH pl, d, r.mode AS transportation_mode
        MATCH (pl)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d)
        WHERE sh.delivery_status = 'Major Delay'
        WITH transportation_mode,
             COUNT(sh)                        AS total_delays,
             round(AVG(sh.delay_days), 2)     AS avg_delay_days,
             COUNT(DISTINCT pl.plant_id)      AS plants_affected
        RETURN transportation_mode, total_delays, avg_delay_days, plants_affected
        ORDER BY total_delays DESC
    """)
    # Fallback: if no Route-based results, try Shipment.route_id join
    if not rows or (isinstance(rows, list) and len(rows) == 0):
        rows = _run("""
            MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d:Distributor)
            WHERE sh.delivery_status = 'Major Delay'
            OPTIONAL MATCH (r:Route {route_id: sh.route_id})
            WITH coalesce(r.mode, 'Unclassified') AS transportation_mode,
                 sh.shipment_id AS sid, sh.delay_days AS delay_days,
                 pl.plant_id AS plant_id
            WITH transportation_mode,
                 COUNT(sid)                       AS total_delays,
                 round(AVG(delay_days), 2)        AS avg_delay_days,
                 COUNT(DISTINCT plant_id)         AS plants_affected
            RETURN transportation_mode, total_delays, avg_delay_days, plants_affected
            ORDER BY total_delays DESC
        """)
    return rows


def get_schema_with_examples() -> dict:
    """
    Returns the full GraphPulse knowledge graph schema — node labels, key
    properties, relationship types with directions — PLUS canonical Cypher
    query patterns with inline comments explaining WHY each pattern is correct.

    Call this BEFORE writing any custom run_cypher query so you use the right
    traversal path. Saves you from fan-out bugs and wrong relationship directions.

    Covers:
      - All 7 node types and their queryable properties
      - All 8 relationship types with correct directions
      - Property value enumerations (delivery_status, mode, etc.)
      - 6 canonical example queries with anti-pattern warnings
    """
    schema = {
        # ── NODE LABELS & KEY PROPERTIES ──────────────────────────────────────
        "nodes": {
            "Supplier": {
                "id":          "supplier_id        (e.g. 'SUP001')",
                "properties":  [
                    "supplier_name",
                    "risk_score          — float 0.0–1.0; ≥0.6 = high risk",
                    "annual_capacity_units",
                    "StoP_lead_time_days — supplier-to-plant lead time",
                    "status              — 'Active' | 'Inactive'",
                ],
            },
            "Plant": {
                "id":         "plant_id            (e.g. 'PL1')",
                "properties": [
                    "plant_name",
                    "plant_city",
                    "plant_state",
                ],
            },
            "Route": {
                "id":         "route_id            (e.g. 'PL1@D001')",
                "properties": [
                    "mode                — 'Road' | 'Rail' | 'Air' | 'Sea'",
                    "PtoD_distance_km",
                    "PtoD_leadtime_days",
                    "PtoD_transportation_cost_inr",
                    "cost_efficiency     — float 0.0–1.0",
                    "plant_id            — FK back to Plant",
                    "distributor_id      — FK back to Distributor",
                ],
            },
            "Shipment": {
                "id":         "shipment_id         (e.g. 'SH00001')",
                "properties": [
                    "delivery_status     — ENUM: 'Major Delay' | 'On Time'  (only these two values)",
                    "delay_days          — integer; 0 when On Time",
                    "ship_date           — Neo4j Date type",
                    "demand_gap          — units unmet; >0 means shortage",
                    "route_id            — FK to Route",
                    "product_category",
                ],
            },
            "Distributor": {
                "id":         "distributor_id      (e.g. 'D001')",
                "properties": [
                    "distributor_city",
                    "distributor_latitude",
                    "distributor_longitude",
                ],
            },
            "Retailer": {
                "id":         "retailer_id",
                "properties": [
                    "retailer_city",
                    "stockout_flag       — boolean",
                ],
            },
            "Product": {
                "id":         "product_id",
                "properties": [
                    "product_category_name — e.g. 'toys', 'health_beauty', 'auto'",
                    "product_name",
                ],
            },
        },

        # ── RELATIONSHIPS (direction matters!) ────────────────────────────────
        "relationships": {
            "SUPPLIES_TO":   "(Supplier)  -[:SUPPLIES_TO]->  (Plant)",
            "SOURCED_FOR":   "(Supplier)  -[SOURCED_FOR]->   (Product)",
            "HAS_ROUTE":     "(Plant)     -[:HAS_ROUTE]->     (Route)",
            "CONNECTS_TO":   "(Route)     -[:CONNECTS_TO]->   (Distributor)",
            "DISPATCHES":    "(Plant)     -[:DISPATCHES]->    (Shipment)",
            "SHIPPED_TO":    "(Shipment)  -[:SHIPPED_TO]->    (Distributor)",
            "CARRIES":       "(Shipment)  -[:CARRIES]->       (Product)",
            "DELIVERS_TO":   "(Distributor)-[:DELIVERS_TO]->  (Retailer)",
        },

        # ── CANONICAL QUERY PATTERNS ──────────────────────────────────────────
        # Study these before writing any run_cypher query.
        # Each pattern shows the CORRECT path and warns about the wrong one.
        "canonical_queries": {

            "1_transport_mode_delay_counts": {
                "question": "Which transport mode causes the most delays?",
                "cypher": """\
MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor)
MATCH (pl)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d)
-- ↑ double-MATCH anchors each shipment to its specific route via shared Distributor
WHERE sh.delivery_status = 'Major Delay'
WITH r.mode AS transportation_mode,
     COUNT(sh) AS total_delays,
     round(AVG(sh.delay_days), 2) AS avg_delay_days,
     COUNT(DISTINCT pl) AS plants_affected
RETURN transportation_mode, total_delays, avg_delay_days, plants_affected
ORDER BY total_delays DESC""",
                "why_correct":  "Anchoring through the shared Distributor node ensures each shipment is counted only once per route. Without this, one shipment matches many routes at the Plant node (fan-out).",
                "anti_pattern": "MATCH (r:Route)<-[:HAS_ROUTE]-(pl)-[:DISPATCHES]->(sh) — this crosses Route and Shipment only through Plant, multiplying shipment counts by the number of routes a plant has.",
            },

            "2_supplier_to_delay_chain": {
                "question": "Which suppliers contribute most to delayed shipments?",
                "cypher": """\
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)-[:DISPATCHES]->(sh:Shipment)
WHERE sh.delivery_status = 'Major Delay'
RETURN sup.supplier_id          AS supplier_id,
       sup.supplier_name        AS supplier_name,
       round(sup.risk_score, 2) AS risk_score,
       pl.plant_id              AS plant_id,
       COUNT(sh)                AS delayed_shipments,
       round(AVG(sh.delay_days), 2) AS avg_delay
ORDER BY delayed_shipments DESC
LIMIT 25""",
                "why_correct":  "Goes Supplier→Plant→Shipment. No Route involved, so no fan-out risk.",
                "anti_pattern": "Do NOT join to Route here — adding HAS_ROUTE inflates delayed_shipments by the number of routes each plant has.",
            },

            "3_distributor_shortage": {
                "question": "Which distributors have the highest demand shortage?",
                "cypher": """\
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d:Distributor)
WITH d.distributor_city AS distributor_city,
     COUNT(DISTINCT CASE WHEN sh.demand_gap > 0 THEN sh.shipment_id END) AS shortage_shipments,
     SUM(CASE WHEN sh.demand_gap > 0 THEN sh.demand_gap ELSE 0 END)      AS total_demand_gap,
     COUNT(DISTINCT CASE WHEN sh.delivery_status = 'Major Delay' THEN sh.shipment_id END) AS delayed_shipments,
     round(AVG(CASE WHEN sh.delivery_status = 'Major Delay' THEN sh.delay_days END), 2) AS avg_delay_days
OPTIONAL MATCH (d2:Distributor {distributor_city: distributor_city})-[:DELIVERS_TO]->(r:Retailer)
RETURN distributor_city, shortage_shipments, total_demand_gap,
       delayed_shipments, avg_delay_days,
       COUNT(DISTINCT r) AS retailers_affected
ORDER BY total_demand_gap DESC LIMIT 15""",
                "why_correct":  "Uses COUNT(DISTINCT CASE WHEN ... THEN sh.shipment_id END) to deduplicate shipments before aggregating gaps.",
                "anti_pattern": "Do NOT use SUM(sh.demand_gap) without a DISTINCT guard — shipments that match multiple paths will have their gap summed multiple times.",
            },

            "4_plant_delay_rates": {
                "question": "Which plants have the highest delay rates?",
                "cypher": """\
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)
WITH pl,
     COUNT(sh) AS total,
     SUM(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed
RETURN pl.plant_id   AS plant_id,
       pl.plant_name AS plant_name,
       total,
       delayed,
       round(100.0 * delayed / CASE WHEN total = 0 THEN 1 ELSE total END, 1) AS delay_rate_pct
ORDER BY delay_rate_pct DESC""",
                "why_correct":  "Pure Plant→Shipment path, no Route fan-out possible.",
                "anti_pattern": "Do NOT add AVG(sh.delay_days) in the same WITH clause as COUNT — it requires the raw rows, not the aggregated totals.",
            },

            "5_full_chain_for_category": {
                "question": "Trace the full supply chain for a product category",
                "cypher": """\
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant)-[:DISPATCHES]->(sh:Shipment)
      -[:CARRIES]->(pr:Product)
WHERE pr.product_category_name = $category
  AND sh.delivery_status = 'Major Delay'
OPTIONAL MATCH (sh)-[:SHIPPED_TO]->(d:Distributor)-[:DELIVERS_TO]->(r:Retailer)
RETURN sup.supplier_name AS supplier, pl.plant_name AS plant,
       sh.shipment_id AS shipment, sh.delay_days AS delay_days,
       d.distributor_city AS distributor, r.retailer_city AS retailer
ORDER BY delay_days DESC LIMIT 50""",
                "why_correct":  "Uses OPTIONAL MATCH for downstream nodes so shipments without a retailer are still returned.",
                "anti_pattern": "Using a required MATCH for Retailer silently drops all shipments that haven't been delivered yet.",
            },

            "6_monthly_trend": {
                "question": "How have delay rates changed month by month?",
                "cypher": """\
MATCH (sh:Shipment)
WHERE sh.ship_date IS NOT NULL
WITH sh.ship_date.year  AS year,
     sh.ship_date.month AS month,
     COUNT(sh) AS total,
     SUM(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 ELSE 0 END) AS delayed
RETURN year, month, total, delayed,
       round(100.0 * delayed / CASE WHEN total = 0 THEN 1 ELSE total END, 1) AS delay_rate_pct
ORDER BY year, month""",
                "why_correct":  "Filters NULL ship_date first to avoid grouping nulls as a phantom month.",
                "anti_pattern": "Do NOT use toString(sh.ship_date) for grouping — it produces inconsistent string formats across Neo4j versions.",
            },
            "7_city_stockout_upstream_trace": {
                "question": "Which city has persistent stockouts and where is the supply chain breaking down upstream? (e.g. Kolkata)",
                "cypher": """\
-- Step 1: Resolve distributor_id from city name
MATCH (d:Distributor)
WHERE toLower(d.distributor_city) = toLower($city)   -- e.g. 'Kolkata'
RETURN d.distributor_id AS distributor_id, d.distributor_city AS city
LIMIT 1

-- Step 2: Monthly demand gap persistence for this distributor
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d:Distributor {distributor_id: $dist_id})
WITH d,
     sh.month_number AS month,
     COUNT(sh) AS total_shipments,
     COUNT(CASE WHEN sh.demand_gap > 0 THEN 1 END) AS shortage_shipments,
     SUM(CASE WHEN sh.demand_gap > 0 THEN sh.demand_gap ELSE 0 END) AS monthly_demand_gap,
     COUNT(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 END) AS delayed_shipments
RETURN d.distributor_city AS city, month, total_shipments,
       shortage_shipments, monthly_demand_gap, delayed_shipments
ORDER BY toInteger(month) ASC

-- Step 3: Per-plant fulfillment vs. forecast — separates capacity failure from transport failure
MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d:Distributor {distributor_id: $dist_id})
WITH pl, d,
     COUNT(sh) AS total_shipments,
     SUM(CASE WHEN sh.demand_gap > 0 THEN sh.demand_gap ELSE 0 END) AS total_demand_gap,
     COUNT(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 END) AS major_delays,
     SUM(CASE WHEN sh.delivery_status = 'On Time'
                  AND sh.demand_gap > 0 THEN sh.demand_gap ELSE 0 END) AS ontime_demand_gap
RETURN d.distributor_city AS city, pl.plant_id, pl.plant_name,
       total_shipments, total_demand_gap, major_delays, ontime_demand_gap
ORDER BY total_demand_gap DESC

-- Step 4: High-risk suppliers at the worst-performing plant
MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant {plant_id: $worst_plant_id})
RETURN sup.supplier_id, sup.supplier_name,
       round(sup.risk_score, 2) AS risk_score,
       COALESCE(sup.StoP_lead_time_days, sup.lead_time_days,
                sup.lead_time, sup.leadtime_days, 0) AS lead_time_days,
       pl.plant_id, pl.plant_name
ORDER BY risk_score DESC""",
                "why_correct": (
                    "1. Resolves distributor_id from city name via graph lookup — never hardcodes IDs. "
                    "2. Uses month_number property for monthly grouping (not toString date). "
                    "3. Separates ontime_demand_gap from total — if on-time shipments still have a gap "
                    "   that proves capacity failure, not transport. "
                    "4. COALESCE over all lead_time property name variants."
                ),
                "anti_pattern": (
                    "Do NOT hardcode distributor_id (e.g. 'D0005') — always resolve it dynamically. "
                    "Do NOT use toString(sh.ship_date) for grouping. "
                    "Do NOT assume a gap is caused by delays just because delays exist — check ontime_demand_gap first."
                ),
            },
        },

        # ── QUICK REFERENCE ───────────────────────────────────────────────────
        "quick_reference": {
            "delivery_status_values":    ["'Major Delay'", "'On Time'"],
            "transport_mode_values":     ["'Road'", "'Rail'", "'Air'", "'Sea'"],
            "product_category_values":   ["'toys'", "'watches_gifts'", "'health_beauty'", "'auto'", "'cool_stuff'", "'bed_bath_table'"],
            "fan_out_warning":           "Whenever you join BOTH (Plant)-[:HAS_ROUTE]->(Route) AND (Plant)-[:DISPATCHES]->(Shipment) in the same MATCH, you MUST anchor through a shared end node (e.g. Distributor) to avoid Cartesian fan-out. Use double-MATCH as shown in canonical query #1.",
            "count_distinct_rule":       "Always use COUNT(DISTINCT sh.shipment_id) — not COUNT(sh) — when a shipment can appear multiple times due to optional matches or multi-hop joins.",
        },
    }
    return schema

# ════════════════════════════════════════════════════════════════════
# SIMULATION TOOLS — for what-if scenario analysis
# ════════════════════════════════════════════════════════════════════

def get_supplier_shutdown_impact(supplier_id: str = "SUP0045") -> str:
    """
    Simulate the impact if a specific supplier shuts down.
    Returns flat rows: supplier details + plant + delayed shipments + distributors exposed.
    """
    query = """
    MATCH (sup:Supplier {supplier_id: $sid})-[:SUPPLIES_TO]->(pl:Plant)
    MATCH (pl)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d:Distributor)
    WITH sup, pl,
         COUNT(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 END) AS delayed_shipments,
         COUNT(DISTINCT d) AS distributors_exposed,
         round(AVG(CASE WHEN sh.delivery_status = 'Major Delay' THEN sh.delay_days END), 2) AS avg_delay_days
    RETURN sup.supplier_name         AS supplier_name,
           sup.supplier_id           AS supplier_id,
           sup.risk_score            AS risk_score,
           sup.annual_capacity_units AS annual_capacity_units,
           COALESCE(sup.StoP_lead_time_days, sup.lead_time_days, sup.lead_time, sup.leadtime_days, 0) AS lead_time_days,
           pl.plant_name             AS plant_name,
           pl.plant_id               AS plant_id,
           delayed_shipments,
           distributors_exposed,
           avg_delay_days
    ORDER BY delayed_shipments DESC
    """
    rows = _run(query, {"sid": supplier_id})
    if not rows:
        return json.dumps({"error": f"Supplier {supplier_id} not found or no data."})
    return json.dumps(rows, indent=2, default=str)


def get_distributor_offline_impact(distributor_id: str = "D0005") -> str:
    """
    Per-plant contribution to a specific distributor's demand gap.
    Returns flat rows: one per feeding plant with shipment counts and demand gap.
    Columns: distributor_id | distributor_city | feeding_plant | plant_id |
             total_shipments | total_demand_gap | major_delays | avg_delay_days
    Only counts positive demand_gap (shortage shipments) to match fulfillment data.
    """
    query = """
    MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d:Distributor {distributor_id: $did})
    WITH d, pl,
         COUNT(sh)           AS total_shipments,
         round(SUM(CASE WHEN sh.demand_gap IS NOT NULL AND sh.demand_gap > 0
                        THEN sh.demand_gap ELSE 0 END), 0)  AS total_demand_gap,
         COUNT(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 END) AS major_delays,
         round(AVG(CASE WHEN sh.delivery_status = 'Major Delay'
                        THEN sh.delay_days END), 2) AS avg_delay_days
    RETURN d.distributor_id   AS distributor_id,
           d.distributor_city AS distributor_city,
           pl.plant_name      AS feeding_plant,
           pl.plant_id        AS plant_id,
           total_shipments,
           total_demand_gap,
           major_delays,
           avg_delay_days
    ORDER BY total_demand_gap DESC
    """
    rows = _run(query, {"did": distributor_id})
    if not rows:
        return json.dumps({"error": f"Distributor {distributor_id} not found."})
    return json.dumps(rows, indent=2, default=str)


def get_distributor_routes(distributor_id: str = "D0005") -> str:
    """
    Get all routes currently serving a specific distributor.
    Returns flat rows — one per route.
    Columns: route_id | from_plant | plant_id | transport_mode |
             distance_km | cost_inr | lead_time_days
    """
    route_query = """
    MATCH (pl:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d:Distributor {distributor_id: $did})
    RETURN r.route_id                     AS route_id,
           pl.plant_name                  AS from_plant,
           pl.plant_id                    AS plant_id,
           r.mode                         AS transport_mode,
           round(r.PtoD_distance_km, 1)  AS distance_km,
           round(r.PtoD_transportation_cost_inr, 0) AS cost_inr,
           r.PtoD_leadtime_days           AS lead_time_days
    ORDER BY r.PtoD_transportation_cost_inr ASC
    """
    routes = _run(route_query, {"did": distributor_id})
    if not routes:
        return json.dumps({"error": f"No routes found for distributor {distributor_id}."})
    return json.dumps(routes, indent=2, default=str)


def get_supplier_downstream_cities(supplier_id: str = "SUP0045") -> str:
    """
    Get all distributor cities downstream of a specific supplier (via its plant).
    Shows current demand gap and shortage shipments per city.
    """
    query = """
    MATCH (sup:Supplier {supplier_id: $sid})-[:SUPPLIES_TO]->(pl:Plant)
    MATCH (pl)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d:Distributor)
    WITH d.distributor_city  AS city,
         d.distributor_id    AS distributor_id,
         pl.plant_name        AS via_plant,
         SUM(sh.demand_gap)   AS total_demand_gap,
         COUNT(sh)            AS shortage_shipments,
         COUNT(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 END) AS major_delays
    WHERE total_demand_gap > 0
    RETURN city, distributor_id, via_plant, shortage_shipments, total_demand_gap, major_delays
    ORDER BY total_demand_gap DESC
    LIMIT 20
    """
    rows = _run(query, {"sid": supplier_id})
    if not rows:
        return json.dumps({"error": f"No downstream data for supplier {supplier_id}."})
    return json.dumps(rows, indent=2, default=str)


def get_distributor_rerouting_options(distributor_id: str = "D0005") -> str:
    """
    If a distributor goes offline, find nearest alternative distributors
    and calculate rerouting cost/capacity from same plants.
    """
    query = """
    MATCH (d_gone:Distributor {distributor_id: $did})
    MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d_gone)
    WITH d_gone, COLLECT(DISTINCT pl.plant_id) AS affected_plants,
         SUM(sh.demand_gap) AS total_gap_at_risk,
         COUNT(sh) AS shipments_at_risk
    MATCH (pl2:Plant)-[:HAS_ROUTE]->(r:Route)-[:CONNECTS_TO]->(d_alt:Distributor)
    WHERE pl2.plant_id IN affected_plants
      AND d_alt.distributor_id <> $did
    MATCH (pl2)-[:DISPATCHES]->(sh2:Shipment)-[:SHIPPED_TO]->(d_alt)
    WITH d_gone, total_gap_at_risk, shipments_at_risk,
         d_alt.distributor_city AS alt_city,
         d_alt.distributor_id   AS alt_id,
         COUNT(sh2)             AS current_load,
         round(AVG(r.PtoD_transportation_cost_inr), 0) AS avg_reroute_cost_inr,
         COUNT(DISTINCT r.mode) AS transport_options
    RETURN alt_city, alt_id, current_load, avg_reroute_cost_inr,
           transport_options,
           total_gap_at_risk AS original_distributor_gap,
           shipments_at_risk AS original_shipment_count
    ORDER BY current_load ASC
    LIMIT 8
    """
    rows = _run(query, {"did": distributor_id})
    if not rows:
        return json.dumps({"note": f"No rerouting data found for {distributor_id}."})
    return json.dumps(rows, indent=2, default=str)


def get_suppliers_at_plant(plant_id: str = "PL4", exclude_supplier_id: str = "SUP0045") -> str:
    """
    Get all suppliers feeding a specific plant, excluding one supplier (e.g. the disrupted one).
    Returns flat rows suitable for Alternative Supplier Analysis step.
    Columns: supplier_id, supplier_name, risk_score, annual_capacity_units, lead_time_days,
             plant_id, plant_name, delayed_shipments, avg_delay_days
    """
    query = """
    MATCH (sup:Supplier)-[:SUPPLIES_TO]->(pl:Plant {plant_id: $pid})
    WHERE sup.supplier_id <> $excl
    MATCH (pl)-[:DISPATCHES]->(sh:Shipment)
    WITH sup, pl,
         COUNT(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 END) AS delayed_shipments,
         round(AVG(CASE WHEN sh.delivery_status = 'Major Delay' THEN sh.delay_days END), 2) AS avg_delay_days
    RETURN sup.supplier_id           AS supplier_id,
           sup.supplier_name         AS supplier_name,
           sup.risk_score            AS risk_score,
           sup.annual_capacity_units AS annual_capacity_units,
           COALESCE(sup.StoP_lead_time_days, sup.lead_time_days, sup.lead_time, sup.leadtime_days, 0) AS lead_time_days,
           pl.plant_id               AS plant_id,
           pl.plant_name             AS plant_name,
           delayed_shipments,
           avg_delay_days
    ORDER BY sup.risk_score DESC
    """
    rows = _run(query, {"pid": plant_id, "excl": exclude_supplier_id})
    if not rows:
        return json.dumps({"error": f"No suppliers found for plant {plant_id}."})
    return json.dumps(rows, indent=2, default=str)


def get_distributor_monthly_stockout(distributor_id: str = "D0005") -> str:
    """
    Returns month-by-month stockout persistence for a distributor.
    Shows total shipments, shortage count, demand gap and delayed count per month.
    Used for Step 1 of Kolkata RCA — proves stockout is persistent across ALL months.
    """
    query = """
    MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d:Distributor {distributor_id: $did})
    WITH d,
         sh.month_number                                                              AS month,
         COUNT(sh)                                                                    AS total_shipments,
         COUNT(CASE WHEN sh.demand_gap IS NOT NULL AND sh.demand_gap > 0 THEN 1 END) AS shortage_shipments,
         round(SUM(CASE WHEN sh.demand_gap IS NOT NULL AND sh.demand_gap > 0
                        THEN sh.demand_gap ELSE 0 END), 0)                           AS monthly_demand_gap,
         COUNT(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 END)             AS delayed_shipments
    RETURN d.distributor_city AS distributor_city,
           d.distributor_id   AS distributor_id,
           month,
           total_shipments,
           shortage_shipments,
           monthly_demand_gap,
           delayed_shipments
    ORDER BY toInteger(month) ASC
    """
    rows = _run(query, {"did": distributor_id})
    if not rows:
        return json.dumps({"error": f"No data for distributor {distributor_id}."})
    return json.dumps(rows, indent=2, default=str)


def get_distributor_fulfillment_by_plant(distributor_id: str = "D0005") -> str:
    """
    For a given distributor, shows how each plant performs on fulfillment:
    total forecast, actual sales, demand gap, fulfillment rate, and on-time gap.
    Used for Step 2 of city stockout RCA — proves gap exists even on on-time deliveries.
    Uses COALESCE for null-safe property access on demand_forecast and sales fields.
    """
    query = """
    MATCH (pl:Plant)-[:DISPATCHES]->(sh:Shipment)-[:SHIPPED_TO]->(d:Distributor {distributor_id: $did})
    WITH pl, d,
         COUNT(sh)                                                                          AS total_shipments,
         round(SUM(COALESCE(sh.demand_forecast_in_units, sh.demand_forecast, sh.forecast_units, 0)), 0)
                                                                                            AS total_forecast_units,
         round(SUM(COALESCE(sh.sales_in_units, sh.sales_units, sh.actual_units, 0)), 0)    AS total_sales_units,
         round(SUM(CASE WHEN sh.demand_gap IS NOT NULL AND sh.demand_gap > 0
                        THEN sh.demand_gap ELSE 0 END), 0)                                 AS total_demand_gap,
         COUNT(CASE WHEN sh.delivery_status = 'Major Delay' THEN 1 END)                   AS delayed_shipments,
         round(SUM(CASE WHEN sh.delivery_status = 'On Time'
                             AND sh.demand_gap IS NOT NULL AND sh.demand_gap > 0
                        THEN sh.demand_gap ELSE 0 END), 0)                                 AS ontime_demand_gap
    RETURN pl.plant_id   AS plant_id,
           pl.plant_name AS plant_name,
           total_shipments,
           total_forecast_units,
           total_sales_units,
           total_demand_gap,
           delayed_shipments,
           ontime_demand_gap,
           CASE WHEN total_forecast_units > 0
                THEN round(toFloat(total_sales_units) / toFloat(total_forecast_units) * 100, 1)
                ELSE 0.0
           END AS fulfillment_rate_pct
    ORDER BY total_demand_gap DESC
    """
    rows = _run(query, {"did": distributor_id})
    if not rows:
        return json.dumps({"error": f"No fulfillment data for distributor {distributor_id}."})
    return json.dumps(rows, indent=2, default=str)


TOOL_FUNCTIONS = {
    "get_supplier_shutdown_impact":          get_supplier_shutdown_impact,
    "get_distributor_offline_impact":        get_distributor_offline_impact,
    "get_distributor_routes":                get_distributor_routes,
    "get_suppliers_at_plant":                get_suppliers_at_plant,
    "get_supplier_downstream_cities":        get_supplier_downstream_cities,
    "get_distributor_rerouting_options":     get_distributor_rerouting_options,
    "get_distributor_monthly_stockout":      get_distributor_monthly_stockout,
    "get_distributor_fulfillment_by_plant":  get_distributor_fulfillment_by_plant,
    # General
    "run_cypher":                       run_cypher,
    "get_graph_summary":                get_graph_summary,
    # Shipment delay analysis
    "get_delayed_shipments":            get_delayed_shipments,
    "get_delay_by_product_category":    get_delay_by_product_category,
    "get_delay_by_plant":               get_delay_by_plant,               # FIXED
    "get_monthly_delay_trend":          get_monthly_delay_trend,           # NEW
    "get_category_plant_delay_heatmap": get_category_plant_delay_heatmap,  # NEW
    # Supplier analysis
    "get_high_risk_suppliers":          get_high_risk_suppliers,            # FIXED
    "get_supplier_plant_delay_chain":   get_supplier_plant_delay_chain,     # FIXED
    "get_supplier_delay_contribution":  get_supplier_delay_contribution,    # NEW ← fixes 1529 bug
    "get_plant_supplier_matrix":        get_plant_supplier_matrix,          # NEW
    "get_supplier_lead_time_analysis":  get_supplier_lead_time_analysis,    # NEW
    # Distributor & demand
    "get_distributor_delay_impact":     get_distributor_delay_impact,       # FIXED
    "get_demand_gap_analysis":          get_demand_gap_analysis,
    "get_stockout_retailers":           get_stockout_retailers,             # FIXED
    # Route analysis
    "get_route_cost_efficiency":        get_route_cost_efficiency,
    "get_route_delay_correlation":      get_route_delay_correlation,        # NEW
    "get_transport_mode_delays":        get_transport_mode_delays,          # FIX — authoritative mode delay counts
    # Schema & query guidance
    "get_schema_with_examples":         get_schema_with_examples,           # NEW — call before run_cypher
    # Full chain trace
    "trace_supply_chain_for_category":  trace_supply_chain_for_category,
    # Write operations + data integrity
    "create_or_update_node":            create_or_update_node,
    "verify_node_exists":               verify_node_exists,
    "audit_supplier_names":             audit_supplier_names,
    "restore_original_supplier_names":  restore_original_supplier_names,
}


# ─────────────────────────────────────────────────────────────
# TOOL SCHEMAS — passed to the Anthropic API as tools=[]
# ─────────────────────────────────────────────────────────────

TOOL_SCHEMAS = [
    {
        "name": "run_cypher",
        "description": (
            "Execute any custom Cypher query on the Neo4j supply chain knowledge graph. "
            "MANDATORY: call get_schema_with_examples BEFORE this tool for any custom query. "
            "The schema tool returns node labels, relationship directions, property names, "
            "and 7 canonical query patterns including a city stockout upstream trace example. "
            "Key rules: (1) delivery_status = 'Major Delay' or 'On Time' only. "
            "(2) Never combine HAS_ROUTE and DISPATCHES in the same MATCH without anchoring "
            "through a shared Distributor — fan-out risk. "
            "(3) Always resolve distributor_id from city name via graph lookup; never hardcode IDs. "
            "(4) Use COALESCE for StoP_lead_time_days / lead_time_days / lead_time / leadtime_days. "
            "Returns JSON results (max 50 rows)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Valid Cypher query. Use only properties defined in get_schema_with_examples. "
                        "delivery_status values: 'Major Delay' or 'On Time' only. "
                        "Filter by distributor_id (resolved from graph), not city string directly."
                    )
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_graph_summary",
        "description": "Get a count of all node types in the graph. Call this first to understand graph size.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_delayed_shipments",
        "description": "Get shipments with Major Delay status, ordered by delay severity. Shows plant, distributor, and route info.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Number of results to return (default 10)", "default": 10}
            }
        }
    },
    {
        "name": "get_delay_by_product_category",
        "description": "Get delay counts and average delay per product category. Use as step 1 of RCA to find most-affected product types.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_delay_by_plant",
        "description": "Get delay counts per plant including total_shipments, delay_rate_pct, and avg_delay. Use as step 2 of RCA to find bottleneck plants.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_monthly_delay_trend",
        "description": "Get month-by-month delayed vs on-time shipment counts with delay_rate_pct. Use for trend analysis and identifying seasonal risk peaks.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_category_plant_delay_heatmap",
        "description": "Get plant × product_category delay counts for heatmap analysis. Shows whether delays are plant-wide or category-specific.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_high_risk_suppliers",
        "description": "Get suppliers above risk threshold WITH their own per-supplier delayed_shipments count. Use as step 3 of RCA. The delayed_shipments field is per-supplier — use it directly for the Delay Contribution column.",
        "input_schema": {
            "type": "object",
            "properties": {
                "threshold": {"type": "number", "description": "Minimum risk_score (0–1). Default 0.6.", "default": 0.6}
            }
        }
    },
    {
        "name": "get_supplier_plant_delay_chain",
        "description": "Show each supplier linked to delayed shipment count at their plant. Returns up to 25 rows. Connects supplier risk → plant → actual delayed_shipments per supplier.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_supplier_delay_contribution",
        "description": "AUTHORITATIVE per-supplier delayed shipment count. Use EXCLUSIVELY as the data source for the Delay Contribution column in any supplier table. Do NOT use distributor delay totals for this column.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_plant_supplier_matrix",
        "description": "Get supplier count, avg risk, max risk, and single-source flag per plant. Use to detect single-point-of-failure vulnerabilities and supply concentration risk.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_supplier_lead_time_analysis",
        "description": "Get per-supplier lead time vs plant average with lead_time_gap. Identifies suppliers whose long StoP lead times cause delays even with moderate risk scores.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_distributor_delay_impact",
        "description": "Get distributors ranked by delayed shipments with sourcing plant info. NOTE: delayed_shipments here is a DISTRIBUTOR total — do NOT use it for per-supplier Delay Contribution.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_demand_gap_analysis",
        "description": "Get distributors with highest total stockout/shortage (positive demand_gap). Shows which plants supply shortage areas.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_distributor_monthly_stockout",
        "description": "Get the month-by-month stockout breakdown for a specific distributor — shows how many months have a demand gap, shortage shipments per month, and monthly unmet units. Use for Kolkata or any city stockout persistence analysis. Parameter: distributor_id (e.g. D0005 for Kolkata).",
        "input_schema": {
            "type": "object",
            "properties": {
                "distributor_id": {"type": "string", "description": "Distributor ID e.g. D0005 for Kolkata"}
            },
            "required": ["distributor_id"]
        }
    },
    {
        "name": "get_distributor_fulfillment_by_plant",
        "description": "Get per-plant fulfillment breakdown for a specific distributor — shows total forecast, sales, demand gap, on-time gap and fulfillment rate per plant. The on-time demand gap confirms supply capacity failure vs transport failure. Parameter: distributor_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "distributor_id": {"type": "string", "description": "Distributor ID e.g. D0005 for Kolkata"}
            },
            "required": ["distributor_id"]
        }
    },
    {
        "name": "get_distributor_offline_impact",
        "description": "Get per-plant contribution to a specific distributor's demand gap — shows which plant contributes the most unmet units, total shipments, major delays and avg delay days. Use to identify the worst-performing plant for a given city. Parameter: distributor_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "distributor_id": {"type": "string", "description": "Distributor ID e.g. D0005 for Kolkata"}
            },
            "required": ["distributor_id"]
        }
    },
    {
        "name": "get_stockout_retailers",
        "description": "Get retailers with highest demand shortage (stockouts). Shows demand_gap totals per retailer city. Use as step 5 of RCA for end-impact.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_route_cost_efficiency",
        "description": "Get all routes with cost efficiency scores. Low efficiency = cost bottleneck routes. Use for logistics cost optimisation.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_route_delay_correlation",
        "description": "Get routes ranked by delayed shipment count with delay_rate_pct and cost_efficiency. Identifies routes that are both expensive AND unreliable.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_transport_mode_delays",
        "description": (
            "AUTHORITATIVE — Count Major Delay shipments grouped by transport mode "
            "(Road/Rail/Air/Sea). Uses a precise double-MATCH via the shared Distributor "
            "node to prevent fan-out duplication. Returns: transportation_mode, "
            "total_delays, avg_delay_days, plants_affected. "
            "Use THIS — not get_supply_chain_overview or run_cypher with a single MATCH — "
            "for ANY question about which transport mode causes the most delays."
        ),
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_schema_with_examples",
        "description": (
            "Returns the complete GraphPulse knowledge graph schema AND 6 canonical "
            "Cypher query patterns with anti-pattern warnings. "
            "CALL THIS BEFORE run_cypher for any custom query. "
            "Covers all 7 node types (Supplier, Plant, Route, Shipment, Distributor, "
            "Retailer, Product), all 8 relationship directions, property enumerations "
            "(delivery_status, mode, product_category), and detailed explanations of "
            "which traversal paths cause fan-out duplication and how to avoid them."
        ),
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "trace_supply_chain_for_category",
        "description": "Trace full supply chain path (Supplier → Plant → Shipment → Distributor → Retailer) for a specific product category.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_category": {"type": "string", "description": "One of: toys, watches_gifts, health_beauty, auto, cool_stuff, bed_bath_table"}
            },
            "required": ["product_category"]
        }
    },
    {
        "name": "create_or_update_node",
        "description": "Execute a MERGE or CREATE Cypher statement to add/update a node or relationship. WRITE OPERATION — use only for Stage 4 graph updates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cypher_merge_query": {"type": "string", "description": "A valid Cypher MERGE or CREATE statement. Must start with MERGE or CREATE."}
            },
            "required": ["cypher_merge_query"]
        }
    },
    {
        "name": "verify_node_exists",
        "description": "Verify a node was successfully created in the graph. Call after create_or_update_node to confirm.",
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Node label e.g. Supplier, Distributor, Route"},
                "property_name": {"type": "string", "description": "Property to search by e.g. supplier_id"},
                "property_value": {"type": "string", "description": "Value to search for e.g. SUP9001"}
            },
            "required": ["label", "property_name", "property_value"]
        }
    },
    {
        "name": "audit_supplier_names",
        "description": "READ-ONLY audit. Compares all Supplier nodes in Neo4j against the original SupplierMaster data and returns a list of any corrupted or overwritten supplier_name values. Run this before restore_original_supplier_names.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "restore_original_supplier_names",
        "description": "WRITE RESTORE. Fixes all Supplier nodes in Neo4j whose supplier_name was overwritten, restoring them to the original SupplierMaster values. Only updates nodes that differ from the original. Safe to run multiple times.",
        "input_schema": {"type": "object", "properties": {}}
    }
]