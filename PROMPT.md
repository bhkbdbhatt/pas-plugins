You are a senior platform engineer building a suite of 7 independent,
sellable plugin modules for US life & annuity Policy Administration
Systems (PAS). Each plugin must:

- Work as a sidecar/middleware layer (NOT replace the core PAS)
- Support: Majesco LifePlus, Oracle OIPA, EIS, McCamish NGIN,
  Accenture ALIP, Sapiens, and any PAS exposing REST/SOAP APIs
- Be deployable on Kubernetes (AWS, GCP, Azure)
- Use OpenAPI 3.1+ specs for all external interfaces
- Support ACORD NGDS (Next-Generation Digital Standards) JSON schemas
  (ref: https://www.acord.org/standards-architecture/acord-data-standards/next-generation-digital-standards)
- Support MCP (Model Context Protocol) for AI agent access
  (ref: https://modelcontextprotocol.io)
- Be multi-tenant with per-carrier isolation
- Include a management UI (Svelte + TypeScript)
- Ship with Docker Compose for local dev, Helm charts for prod
- Include comprehensive OpenAPI specs, Postman collections, and
  contract tests (Dredd or Schemathesis)

---

## PLUGIN 1: AI-Ready API Gateway & MCP Orchestrator

### Problem
Legacy PAS APIs are monolithic, use proprietary DSLs, and are not
discoverable by AI agents. Reference the "atomicity" principle from:
- https://coverager.com/your-pas-has-apis-theyre-not-ai-ready/
- https://insuremo.com/en/blog/what-ai-ready-insurance-apis-actually-look-like

### Architecture
Build a gateway layer with these components:

1. **API Discovery & Translation Engine**
   - Ingest any PAS's existing API (SOAP, REST, proprietary)
   - Decompose into atomic, single-purpose OpenAPI 3.1 endpoints
   - Example: `POST /insurance/v1/policies/{id}/premium/calculate`
   - Each endpoint must have:
     - Self-describing parameters (no magic flags)
     - Full request/response JSON Schema
     - Example payloads
     - Error code catalog
   - Reference: OpenAPI 3.2.0 spec (Sept 2025)
     https://www.openapis.org/specification/openapi

2. **MCP Server Layer**
   - Expose all atomic operations as MCP tools
   - AI agents (Claude, GPT, enterprise LLMs) can discover and
     call them without custom integration
   - Reference: ACORD Solutions Group MCP architecture (May 2026)
     https://www.prnewswire.com/news-releases/insurance-industry-is-now-agentic-ai-ready-with-mcp-architecture-from-acord-solutions-group-302784267.html
   - Reference: One Inc MCP for insurance payments (Feb 2026)
     https://www.oneinc.com/resources/news/one-inc-unveils-model-context-protocol-to-accelerate-insurance-payments-integration-and-secure-ai-data-access
   - Use FastAPI + MCP SDK (Python) or @modelcontextprotocol/sdk (TypeScript)

3. **Orchestration Engine**
   - Compose atomic operations into business workflows
   - Example: `createQuote → validateEligibility → calculatePremium → bindPolicy`
   - Use Apache Airflow or Temporal for workflow orchestration
   - Reference: https://blog.postman.com/ai-ready-apis-agentic-ai-aws-competency/

4. **Security & Governance**
   - OAuth 2.1 + OIDC (per MCP spec recommendation, March 2025)
   - Per-tenant rate limiting, audit logging
   - Reference: https://api7.ai/blog/open-insurance-apis-security-compliance-traffic-governance

### Tech Stack
- Gateway: Kong or Zuplo (ref: https://zuplo.com/learning-center/best-api-gateways-ai-llm-workloads-2026)
- MCP Server: Python FastAPI + mcp package
- Workflow: Temporal (open source) or AWS Step Functions
- Storage: PostgreSQL (tenant metadata), Redis (caching)
- Docs: ReadMe or GitBook with OpenAPI sync
  (ref: https://buildwithfern.com/post/insurance-api-documentation-platforms-comparison)

### ACORD NGDS Integration
- Map all atomic operations to ACORD NGDS JSON structures
- Support Life & Annuity transaction types (TX-103, etc.)
- Reference: https://www.acord.org/standards-architecture/get-involved/standards-project-advisory-groups
- Reference: https://apis.io/providers/underwriting-standards/

### Deliverables
- [ ] OpenAPI 3.1 spec for all atomic operations (YAML)
- [ ] MCP server with tool definitions
- [ ] Translation rules engine (YAML/JSON config per PAS vendor)
- [ ] Helm chart + Docker Compose
- [ ] Postman collection with 50+ example requests
- [ ] Contract tests (Schemathesis)
- [ ] Management UI: API catalog, tenant config, monitoring
- [ ] Documentation site (ReadMe/GitBook)

### Pricing Model
- Per-PAS-integration license: $50K–$150K
- Per-API-call tier: $0.01–$0.05/call
- MCP access: $5K–$20K/month per carrier

---

## PLUGIN 2: IFRS 17 / Regulatory Automation Engine

### Problem
<10% of insurers have fully automated IFRS 17. ~50% still use
manual intervention. No PAS vendor ships this natively.
Reference:
- IFRS 17 CSM calculation: https://www.actuaries.org.uk/documents/ifrs17csmcalculating-value-initial-recognition20190923
- CSM roll-forward methodology: https://www.efrag.org/sites/default/files/sites/webpublishing/SiteAssets/IFRS%2017%20Background%20briefing%20paper%20CSM%20allocation.pdf
- Grant Thornton GMM guide: https://www.grantthornton.in/globalassets/1.-member-firms/india/assets/pdfs/financial-services-knowledge-series-on-ifrs-17-general-measurement-model-volume-ii.pdf

### Architecture

1. **CSM Calculation Engine**
   - Implement GMM (General Measurement Model), VFA (Variable Fee
     Approach), PAA (Premium Allocation Approach)
   - CSM = PV(future inflows) – PV(future outflows) – Risk Adjustment
     + DAC – cash at recognition
   - CSM roll-forward: opening + new business + interest accretion
     – release to P&L – changes in FCF
   - Reference open-source implementations:
     - https://github.com/Systemorph/IFRS17CalculationEngine
     - https://github.com/seokhoonj/fastcashflow
     - https://github.com/prithiyangabintumani/https-github.com-prithiyangabintumani-IndAS117-CSM-Model
   - Commercial reference: https://fineit.io/solutions/estimator-17

2. **GIC (Group of Insurance Contracts) Tagging**
   - Auto-classify policies into GICs based on:
     - Product type, cohort year, profitability
   - Configurable rules engine (Drools or custom Python DSL)
   - Reference: IFRS 17 paragraph 4 (grouping criteria)

3. **Cash Flow Projection Engine**
   - Actuarial assumptions: mortality, lapse, expense, discount rate
   - Monte Carlo simulation for RA (Risk Adjustment)
   - Support for locked-in vs. updated discount rates
   - Use Python: NumPy, SciPy, or R (via rpy2)

4. **Disclosure & Reporting Generator**
   - Auto-generate IFRS 17 disclosure tables
   - CSM roll-forward table (per EFRAG example)
   - Liability roll-forward by line of business
   - NAIC SSAP 102 (US) mapping
   - Output: Excel, PDF, XBRL

5. **PAS Integration Layer**
   - Connect to any PAS via API (use Plugin 1's atomic operations)
   - Pull policy data, premiums, claims, lapses
   - Push calculated reserves back to PAS

### Tech Stack
- Core: Python 3.12 (NumPy, SciPy, pandas)
- Rules: Drools (Java) or PyKE (Python)
- DB: PostgreSQL (policy data, assumption versions)
- Compute: Dask or Apache Spark (for large portfolios)
- UI: React + Recharts (CSM waterfall, sensitivity tornado charts)
- API: FastAPI + OpenAPI 3.1

### Deliverables
- [ ] CSM calculation engine (GMM, VFA, PAA)
- [ ] GIC tagging rules engine
- [ ] Cash flow projection with Monte Carlo RA
- [ ] Disclosure generator (IFRS 17, SSAP 102)
- [ ] PAS connector (Majesco, Oracle OIPA, EIS, McCamish)
- [ ] Assumption management UI (versioning, sensitivity)
- [ ] Audit trail (every calculation step logged)
- [ ] Helm chart + Docker Compose

### Pricing Model
- Per-carrier license: $100K–$300K/year
- Per-policy-volume tier
- Implementation: $50K–$200K (one-time)

---

## PLUGIN 3: AI-Powered Accelerated Underwriting (AUW) Workbench

### Problem
38% of carriers cite legacy IT as primary underwriting challenge.
45% say self-service tools would most impact speed.
Reference:
- https://www.scnsoft.com/insurance/artificial-intelligence/underwriting
- https://acquaintsoft.com/blog/insurance-underwriting-platform-development
- https://ask-luca.com/blogs/ai-underwriting
- AWS reference architecture: https://aws.amazon.com/blogs/machine-learning/streamline-insurance-underwriting-with-generative-ai-using-amazon-bedrock-part-1/

### Architecture

1. **Submission Intake & Triage**
   - Ingest applications from any PAS (via Plugin 1 atomic APIs)
   - NLP extraction from unstructured docs (PDF, images)
   - Use: AWS Textract / Google Document AI / Azure Form Recognizer
   - Complexity scoring: route to auto-decide, fast-track, or full UW

2. **Data Enrichment Layer**
   - MIB (Medical Information Bureau) records
   - Rx (prescription) aggregators
   - Digital health questionnaires (NLP-processed)
   - External APIs: LexisNexis, CAPE, telematics
   - Reference: https://acquaintsoft.com/blog/insurance-underwriting-platform-development
     (tech stack: XGBoost, LightGBM, PyTorch for scoring)

3. **ML Risk Scoring Engine**
   - Gradient boosted trees (XGBoost/LightGBM) for structured data
   - Neural networks for unstructured/sequential inputs
   - Logistic regression challenger model (regulatory baseline)
   - Feature store: Feast + Redis
   - Reference: https://kanopylabs.com/blog/ai-for-insurance-underwriting-claims-optimization

4. **Rules Engine & Decision API**
   - Drools or custom Python DSL for appetite/eligibility rules
   - Versioned, auditable, underwriter-readable
   - Decision API: `POST /uw/v1/decisions` → accept/decline/refer + reason code
   - Reference: https://www.hyperexponential.com/blog/agentic-ai-insurance-underwriting

5. **Underwriter Decision Support UI**
   - React dashboard: risk score, explanation (SHAP values),
     document highlights, similar case comparison
   - Override workflow with full audit trail
   - Reference: https://appinventiv.com/blog/ai-in-insurance-underwriting-process/

6. **MCP Integration**
   - Expose underwriting rules, decision history, and
     recommendation engine via MCP
   - Reference: https://www.neutrinos.com/resource-hub/model-context-protocol-escaping-insurance-ai/

### Tech Stack
- ML: Python (XGBoost, LightGBM, PyTorch, scikit-learn)
- MLOps: MLflow or SageMaker
- Rules: Drools (Java) or PyKE
- Inference API: FastAPI
- Feature Store: Feast + Redis
- Document AI: AWS Textract or Azure Form Recognizer
- Workflow: Apache Airflow
- DB: PostgreSQL (decisions, audit), S3 (documents)
- UI: React + Django REST + SHAP visualization
- Event streaming: Apache Kafka (continuous UW signals)

### Deliverables
- [ ] Submission triage + NLP document extraction
- [ ] Data enrichment connectors (MIB, Rx, health)
- [ ] ML scoring pipeline (train, serve, monitor)
- [ ] Rules engine with versioning
- [ ] Decision API + audit log
- [ ] Underwriter dashboard (React)
- [ ] MCP server for underwriting tools
- [ ] Model monitoring (drift detection, retraining triggers)
- [ ] Helm chart + Docker Compose

### Pricing Model
- Per-underwriter-seat: $5K–$15K/month
- Per-application-processed: $1–$5
- Implementation: $100K–$300K

---

## PLUGIN 4: Low-Code Product Configuration Engine

### Problem
6+ months to launch a new product/rider. Business users locked out.
Reference:
- Sapiens 2026 migration guide (business-user enablement)
- ACORD NGDS product structures:
  https://www.acord.org/standards-architecture/get-involved/standards-project-advisory-groups

### Architecture

1. **Visual Product Builder**
   - Drag-and-drop UI to define:
     - Product types (Term, Whole Life, IUL, RILA, LTC)
     - Riders and guaranteed living benefits
     - Rating factors, premium tables
     - Eligibility rules, exclusions
   - Map to ACORD NGDS product JSON structures
   - Reference: https://www.acord.org/standards-architecture/acord-data-standards/next-generation-digital-standards

2. **Rule-Based Configuration**
   - No-code rule editor (visual decision trees)
   - Versioning, diff, rollback
   - Test sandbox: simulate policies against new config
   - Reference: https://acquaintsoft.com/blog/insurance-underwriting-platform-development
     (product configuration: DB-driven config tables)

3. **Deployment Pipeline**
   - Config → CI/CD → PAS (via Plugin 1 atomic APIs)
   - Staging → Production with approval workflow
   - Auto-generated OpenAPI spec updates

4. **Compliance Validation**
   - State-by-state illustration compliance checks
   - NAIC model regulation validation
   - Auto-flag regulatory gaps before deployment

### Tech Stack
- UI: React + Node.js (visual builder, rule editor)
- Backend: Python (FastAPI) or Java (Spring Boot)
- Rules: Drools or custom DSL
- Config storage: PostgreSQL (JSONB for ACORD NGDS structures)
- CI/CD: ArgoCD + GitHub Actions
- Testing: Schemathesis (contract), pytest (unit)

### Deliverables
- [ ] Visual product builder (React)
- [ ] Rule editor with versioning
- [ ] ACORD NGDS mapping layer
- [ ] Test sandbox + simulation
- [ ] CI/CD pipeline to PAS
- [ ] Compliance validation engine
- [ ] Management UI + audit log
- [ ] Helm chart + Docker Compose

### Pricing Model
- Per-product-configured: $5K–$20K/month
- Per-carrier platform license: $100K–$250K/year
- Implementation: $75K–$200K

---

## PLUGIN 5: Embedded Insurance / API-First Distribution Plugin

### Problem
~$70B global embedded premium opportunity by 2026. Life/annuity
PAS can't handle sub-second response or white-label configs.
Reference:
- https://www.soa.org/sections/marketing-distribution/marketing-distribution-newsletter/2025/april/nd-2025-04-li/
- https://www.everlylife.com/ (embedded life for banks/RIAs)
- https://hexure.com/apis/ (FireLight embedded APIs)
- https://rootplatform.com/embedded-insurance
- https://insurtechdigital.com/articles/eleos-life-enters-us-market-with-embedded-insurance-apis

### Architecture

1. **White-Label Product Engine**
   - Partner configures: product selection, branding, pricing
     overrides, distribution rules
   - Per-partner API keys, rate limits, revenue share
   - Reference: https://sandis.io/platform/white-label-platform

2. **Sub-Second Quote-to-Bind Pipeline**
   - `POST /embed/v1/quote` → <500ms response
   - `POST /embed/v1/bind` → policy issued, <2s
   - Caching layer (Redis) for product configs, rating tables
   - Async: underwriting, policy issuance, document generation
   - Reference: https://sandis.io/platform/embedded-insurance-api
     (3-week deployment methodology)

3. **Partner Portal**
   - Self-service: product catalog, API keys, analytics
   - White-label widget embed (JavaScript SDK)
   - Reference: https://www.qover.com/api (white-label + API)

4. **Transaction Monitoring & Settlement**
   - Per-transaction logging, revenue share calculation
   - Real-time dashboard: quotes, binds, conversion, premium
   - Settlement engine: monthly/weekly partner payouts

### Tech Stack
- API: Go (Gin) or Node.js (Fastify) for low-latency
- Cache: Redis
- DB: PostgreSQL (partner configs, transactions)
- Async: Apache Kafka (underwriting, doc gen)
- SDK: JavaScript (React/Vue) widget + REST client
- UI: React (partner portal)
- Monitoring: Prometheus + Grafana

### Deliverables
- [ ] White-label product config engine
- [ ] Sub-second quote/bind API (OpenAPI 3.1)
- [ ] JavaScript embeddable widget (React)
- [ ] Partner portal (self-service)
- [ ] Revenue share + settlement engine
- [ ] Transaction monitoring dashboard
- [ ] Helm chart + Docker Compose
- [ ] Partner onboarding docs + sandbox

### Pricing Model
- Per-partner: $10K–$50K/month
- Per-transaction: $0.50–$5.00
- Platform setup: $50K–$150K

---

## PLUGIN 6: Unified Data Foundation / AI-Ready Data Mesh

### Problem
Customer data siloed across policy, claims, billing, CRM.
54% of carriers spend >50% IT budget maintaining existing systems.
Reference:
- https://www.scnsoft.com/insurance/artificial-intelligence/underwriting
  (data lake + warehouse architecture)
- https://newgensoft.com/insurance-underwriting/

### Architecture

1. **Data Ingestion Layer**
   - Connect to any PAS, claims, billing, CRM via API
   - CDC (Change Data Capture) for real-time sync
   - Batch + streaming (Kafka) ingestion
   - Reference: https://acquaintsoft.com/blog/insurance-underwriting-platform-development
     (event streaming: Apache Kafka)

2. **Data Unification & Governance**
   - Master Data Management: customer, policy, product
   - Data quality rules, lineage tracking
   - Reference: ACORD NGDS as canonical data model
     https://www.acord.org/standards-architecture/acord-data-standards

3. **Feature Store for AI**
   - Feast (open source) + Redis for low-latency serving
   - Feature definitions, versioning, monitoring
   - Reference: https://ask-luca.com/blogs/ai-underwriting
     (7-layer architecture, feature store layer)

4. **API Access Layer**
   - All unified data exposed via REST + MCP
   - AI agents can query customer 360, policy history,
     claims, billing in one call
   - Reference: https://winsurtech.com/blog/model-context-protocol/

5. **Audit & Compliance**
   - Full interaction history (MCP-style)
   - Data access logging, PII masking
   - SOC 2, HIPAA-ready

### Tech Stack
- Ingestion: Apache Kafka + Debezium (CDC)
- Storage: Apache Iceberg (on S3/GCS) + PostgreSQL
- Feature Store: Feast + Redis
- Query: Apache Spark (batch), Trino (interactive)
- API: FastAPI + MCP server
- Governance: OpenLineage (data lineage)
- UI: React (data catalog, lineage graph, quality dashboards)

### Deliverables
- [ ] Multi-source ingestion (Kafka + Debezium)
- [ ] MDM + data quality engine
- [ ] Feature store (Feast)
- [ ] Unified data API + MCP server
- [ ] Lineage + audit dashboard
- [ ] PII masking + access control
- [ ] Helm chart + Docker Compose

### Pricing Model
- Per-carrier platform: $200K–$500K/year
- Per-GB-ingested: $5–$20/GB
- Implementation: $150K–$400K

---

## PLUGIN 7: Blockchain-Based Policy Lifecycle Layer

### Problem
Fragmented policy records, slow beneficiary updates, annuity
record management, cross-vendor continuity.
Reference:
- https://www.ifrs.org/content/dam/ifrs/groups/ifric/requests-to-be-considered-at-a-future-committee-meeting/submission-on-amortisation-of-contractual-service-margin-for-annuity-contracts-ifrs-17.pdf
  (annuity lifecycle complexity)

### Architecture

1. **Permissioned Blockchain Network**
   - Hyperledger Fabric (enterprise, permissioned)
   - Nodes: carrier, regulator, beneficiary, service provider
   - Reference: Hyperledger Fabric docs
     https://hyperledger-fabric.readthedocs.io

2. **Smart Contracts (Chaincode)**
   - Policy lifecycle: issue → modify → lapse → terminate → claim
   - Beneficiary update: multi-sig approval, immutable record
   - Annuity payout: automated schedule, audit trail
   - Cross-vendor continuity: policy record portability
     during system migrations

3. **PAS Integration**
   - Plugin 1 atomic APIs → blockchain events
   - Every policy state change written to chain
   - Query API: `GET /blockchain/v1/policies/{id}/history`

4. **Digital Identity Layer**
   - Self-sovereign identity (SSI) for beneficiaries
   - DID (Decentralized Identifiers) + Verifiable Credentials
   - Reference: W3C DID spec https://www.w3.org/TR/did-core/

### Tech Stack
- Blockchain: Hyperledger Fabric 2.x
- Chaincode: Go or Node.js
- Identity: Hyperledger Aries (SSI)
- API: FastAPI (query layer)
- DB: CouchDB (Fabric state), PostgreSQL (metadata)
- UI: React (policy history, beneficiary management)

### Deliverables
- [ ] Hyperledger Fabric network (multi-org)
- [ ] Chaincode: policy lifecycle, beneficiary, annuity
- [ ] SSI/DID integration for beneficiaries
- [ ] PAS connector (via Plugin 1)
- [ ] Query API + MCP server
- [ ] Admin UI (network, policies, identity)
- [ ] Helm chart + Docker Compose

### Pricing Model
- Per-carrier network: $150K–$400K/year
- Per-policy-on-chain: $1–$5/year
- Implementation: $200K–$500K

---

## SHARED INFRASTRUCTURE (All Plugins)

### Common Components
- **Auth**: Keycloak (OAuth 2.1 + OIDC)
- **API Gateway**: Kong or Zuplo
- **Service Mesh**: Istio (mTLS, traffic management)
- **Observability**: Prometheus + Grafana + Jaeger (tracing)
- **CI/CD**: GitHub Actions + ArgoCD
- **IaC**: Terraform (AWS/GCP/Azure)
- **Secrets**: HashiCorp Vault
- **Docs**: ReadMe (OpenAPI sync) + GitBook
- **Testing**: Schemathesis (contract), pytest, Jest, k6 (load)

### ACORD NGDS Compliance (All Plugins)
- All data models mapped to ACORD NGDS JSON schemas
- Reference: https://www.acord.org/standards-architecture/acord-data-standards/next-generation-digital-standards
- Life & Annuity subgroup standards:
  https://www.acord.org/standards-architecture/get-involved/standards-project-advisory-groups
- ACORD Solutions Group MCP architecture (May 2026):
  https://www.prnewswire.com/news-releases/insurance-industry-is-now-agentic-ai-ready-with-mcp-architecture-from-acord-solutions-group-302784267.html

### MCP Integration (All Plugins)
- Every plugin exposes an MCP server
- Tools, resources, and prompts defined per MCP spec
- Reference: https://modelcontextprotocol.io
- Insurance-specific MCP examples:
  - https://winsurtech.com/blog/model-context-protocol/
  - https://www.neutrinos.com/resource-hub/model-context-protocol-escaping-insurance-ai/
  - https://www.oneinc.com/resources/news/one-inc-unveils-model-context-protocol-to-accelerate-insurance-payments-integration-and-secure-ai-data-access

### Build Order (Dependency Chain)
1. **Plugin 1** (API Gateway + MCP) — foundation for all others
2. **Plugin 6** (Data Mesh) — data layer for AI plugins
3. **Plugin 2** (IFRS 17) — highest revenue clarity
4. **Plugin 3** (AUW Workbench) — proven ROI
5. **Plugin 4** (Low-Code Config) — SaaS model
6. **Plugin 5** (Embedded Distribution) — big prize
7. **Plugin 7** (Blockchain) — highest risk, longest adoption

### Funding Pitch Template
"We build the missing layer between AI and legacy insurance cores.
No core replacement required. 90% of carriers are piloting AI.
The gap is architectural, not aspirational. We ship 7 plugins that
make any PAS AI-ready, compliant, and distribution-ready in weeks,
not quarters."

### Reference Architecture Diagram
[Generate a C4 model diagram showing:
- Context: Carrier (PAS), AI Agents, Partners, Regulators
- Container: 7 plugins + shared infra
- Component: Key modules within each plugin
- Code: Core algorithms (CSM, ML scoring, chaincode)]   