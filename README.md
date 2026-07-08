GraphPulse AI: AI-Powered Supply Chain Intelligence Platform using Knowledge Graphs, Multi-Agent AI & Neo4j

GraphPulse AI is an AI-powered supply chain intelligence platform that transforms traditional logistics data into an intelligent Knowledge Graph capable of answering natural language questions, performing automated Root Cause Analysis (RCA), visualizing supply chain health, and safely updating graph data through AI-powered workflows.

Built as part of a Transportation & Logistics use case, the project combines Knowledge Graphs, Large Language Models (LLMs), Agentic AI, Neo4j, and Model Context Protocol (MCP) into a single intelligent analytics platform.

Project Overview

Modern supply chains generate huge amounts of interconnected data, making it difficult to identify the real causes of delays, stockouts, and operational bottlenecks.

GraphPulse AI addresses this challenge by:

- Converting logistics datasets into a Neo4j Knowledge Graph
- Allowing users to query the graph using natural language
- Automatically generating Cypher queries using LLMs
- Performing AI-driven multi-agent Root Cause Analysis
- Visualizing supply chain performance
- Updating the graph through AI-assisted workflows

Key Features
- Knowledge Graph Construction
- Builds a Neo4j Knowledge Graph from CSV datasets
- Models Suppliers, Plants, Distributors, Retailers, Products, Routes and Shipments
- Captures complex multi-hop supply chain relationships
- Natural Language Querying
- Converts user questions into Cypher queries using LLMs
- Executes queries on Neo4j
- Returns structured tables with AI-generated insights
- Multi-Agent Root Cause Analysis

A specialized AI pipeline investigates supply chain disruptions through multiple collaborating agents.

The pipeline:
- Selects relevant analytical tools
- Collects graph evidence
- Validates retrieved data
- Performs root cause analysis
- Generates business recommendations
- Produces executive summaries
- Interactive Dashboard
- Supply Network Health Score
- Delay Analysis
- Monthly Trend Analysis
- Route Performance
- Demand Gap Analysis
- Transport Mode Insights
- Intelligent Graph Updates

Supports both:

Natural language graph updates
CSV-based bulk updates

Includes:

- Schema validation
- AI field mapping
- Conflict detection
- Dry-run simulation
- Rollback support
- Automatic CSV synchronization

Multi-Agent AI Pipeline

GraphPulse AI employs a multi-agent AI architecture where specialized agents collaborate to perform comprehensive supply chain analysis. Instead of relying on a single LLM, the system decomposes complex analytical tasks into dedicated responsibilities, enabling more accurate, reliable, and explainable results.

The pipeline consists of five specialized agents:

- Orchestrator Agent – Interprets the user's query, selects the most relevant analytical tools, and coordinates the overall investigation by gathering evidence from the Knowledge Graph.
- Data Validator Agent – Reviews and validates the retrieved data, removes inconsistent or incomplete results, and ensures that downstream analysis is based on reliable information.
- Root Cause Analysis (RCA) Agent – Synthesizes validated evidence to identify bottlenecks, supplier risks, plant delays, demand gaps, and the underlying causes of supply chain disruptions.
- Recommendations Agent – Generates actionable, data-driven recommendations with prioritized corrective actions to improve operational efficiency and mitigate future risks.
- Narrative Agent – Produces concise executive summaries that transform technical findings into business-friendly insights for decision-makers.

The agents communicate through a shared context object, allowing each stage to build upon the previous one while maintaining modularity and reducing hallucinations. Tool execution is handled via the Model Context Protocol (MCP), enabling secure interaction with Neo4j analytical tools and ensuring that insights remain grounded in real graph data rather than model assumptions. This collaborative architecture enables GraphPulse AI to deliver transparent, explainable, and end-to-end supply chain intelligence
