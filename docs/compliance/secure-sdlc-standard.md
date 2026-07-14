# Secure Software Development Lifecycle (SDLC) Standard

*An industry-agnostic, consequence-first standard for software however authored — human, AI-assisted, agentic, or end-user.*
*v1.0 — July 2026 · © 2026 NaanyaBiz — published under this repository's licence.*
*This repository operates under this standard; the statement-level conformance record is [conformance.md](./conformance.md).*

---

## 1. Purpose and scope

This standard sets the mandatory control objectives and control statements governing how software is designed, built, changed, deployed, and retired. It applies to **all software the organisation creates or materially modifies**, irrespective of who or what authored it — internal engineers, AI coding assistants, autonomous agents, or user developers — whose user-developed applications (UDAs) span code-class artifacts (e.g. analyst / data-science Python, R, SQL), low-code/no-code platform apps, and document-embedded solutions such as spreadsheets and kindred office-productivity artifacts (Section 6) — and irrespective of the runtime (on-prem, cloud, container, serverless, edge). It is written to scale by consequence, not by industry or size: the same objectives apply, at proportionate intensity, from a single-maintainer open-source project to a large regulated enterprise. Agentic systems and their configuration artifacts (prompts, skills, tool and connector definitions) are in scope as software (CO-12).

It does **not** restate controls owned by sibling standards; it defines the seams (Section 4).

## 2. Position in the standards hierarchy

```
Technology Lifecycle Standard (or equivalent policy root)
├── SDLC Standard ........................... (this document)
├── Change & Release Management Standard
├── Cloud & Infrastructure Standard
├── Identity & Access Management Standard
├── Vulnerability & Threat Management Standard
├── Third-Party / Supplier Risk Standard
├── Data Management & Classification Standard
└── Model Risk Management Standard
```

This hierarchy is a **reference architecture**, not a mandated document count: smaller organisations may collapse the sibling standards into brief policy statements — a paragraph can stand where an enterprise needs a volume — but each seam in Section 4 still needs an answer, however lightweight.

## 3. Design philosophy (why this differs from a legacy SDLC standard)

| Legacy assumption | Modernised control basis |
|---|---|
| Sequential phases with human stage-gate sign-offs | Continuous controls enforced as **policy-as-code** in the pipeline; the pipeline is the control plane and the evidence store |
| Code is written by employees | **Author-agnostic** controls; AI-generated, agent-authored, and user-developed software (UDAs) are in scope |
| Periodic, point-in-time security testing | Continuous, automated assurance gating every change |
| Third-party/OSS code is trusted on assertion | **Software supply chain** treated as adversary-relevant; provenance, SBOM, signing, attestation |
| Uniform rigor for all software | Control intensity **proportionate to consequence** (criticality tier × data sensitivity × reversibility) |
| Change is a discrete, manually-approved event | Deployment safety engineered in (progressive delivery, automated rollback); standard changes pre-approved by pipeline conformance |
| Software is either professionally engineered or out-of-scope "shadow IT" | Authoring architecture is **stratified** (S1–S5, Section 6): objectives invariant, control *expression* and tier *eligibility* vary; UDAs and agents are governed in-scope populations |

**Framework basis.** The standard draws on two distinct classes of framework, which must not be conflated.

*Control-source frameworks* generate the control statements: NIST CSF 2.0 (GV/ID/PR/DE/RS/RC) for the governance and risk wrap; NIST SSDF SP 800-218 (PO/PS/PW/RV) for engineering practices, with SP 800-218A for the generative-AI / foundation-model overlay; SLSA for build and provenance integrity; OWASP ASVS for control depth; ISO/IEC 25010 (product-quality model — incl. the 2023 revision elevating security and safety) and ISO/IEC 5055 / CISQ (structural code-quality measures) for software quality; ITIL 4 for change enablement and problem management; and, for workloads subject to a prescriptive sector regime, that regime's lifecycle obligations (e.g. IEC 62304 for medical-device software, ISO 26262 for automotive, DO-178C for aviation, MiFID II RTS 6 (Reg. (EU) 2017/589) for algorithmic trading). Operational-resilience alignment follows the organisation's applicable operational-resilience regulation (e.g. EU DORA for financial entities, or the sector's equivalent resilience regime).

*Process-architecture and maturity frameworks* shape, govern, and assess the standard but do **not** generate control statements — treating them as a control catalogue is a category error:
- **ISO/IEC/IEEE 15288** (system lifecycle processes) frames the parent **Technology Lifecycle Standard**; **ISO/IEC/IEEE 12207** (software lifecycle processes) frames *this* standard — 12207 is the software-specific elaboration of the 15288 system frame.
- **ISO 9001** principles govern the standard *as a controlled management-system artifact*: documented, version-controlled, periodically reviewed and internally audited, with corrective action and continual improvement (CO-1.5, CO-18).
- **CMMI** provides the process-maturity assessment lens and **BSIMM** the descriptive benchmark for software-security-program maturity (pairing with the prescriptive OWASP SAMM); adoption of this standard is *assessed* against these, not *defined* by them.

**Lifecycle-approach neutrality.** The standard is deliberately **methodology-agnostic** — it does not mandate or differentiate controls by lifecycle approach (Waterfall, Spiral, Agile/Scrum/Kanban/SAFe, DevSecOps, SRE). These are not even the same class of thing (sequencing models vs. a delivery philosophy vs. operating/engineering models that wrap a delivery flow), and **control objectives are outcomes while the approach is the means**: *"changes are independently reviewed, tested, and provably built before release"* holds identically whichever model a team uses — only the *control point and the form of evidence* change (a phase-gate sign-off under Waterfall; an automated pipeline gate result under continuous delivery). Three consequences follow:
- **The axis of control differentiation is criticality tier (Section 5), not methodology.** A "Waterfall control set" or "Agile control set" would duplicate, badly, what tiering already does.
- **Control and gate are decoupled.** Assurance must accept the evidence form native to the delivery model; standards that implicitly assume stage-gate sign-offs force Agile/DevOps teams into either non-compliance or compliance theatre. Cadence and automation of enforcement scale with the model (per-commit under continuous delivery; at phase boundaries under Waterfall) — coverage must be complete either way.
- **Methodology is governed through approved delivery-model paved roads (CO-2.5)** — a small pre-accredited set (e.g. cloud-native CI/CD, mainframe/COTS release, low-code), each a pre-wired implementation of the same objectives. SRE's SLO/error-budget governance is an accepted — indeed preferred — means of meeting the resilience and operability objectives (CO-16), not a separate regime.

The strategic default is **DevSecOps / continuous delivery** (the pipeline as control plane and evidence store, per the table above); the standard nonetheless remains approach-agnostic so that legacy, mainframe, COTS, and genuinely non-incremental workloads comply via their native gate mechanisms — with the trajectory being migration onto the automated paved roads (and the CO-10 exception path available for justified big-bang deployments under compensating controls).

**Authoring-architecture stratification (second proportionality axis).** Lifecycle neutrality governs *how delivery is sequenced*; a second axis governs *what control surface the authoring architecture exposes*. Software is assigned to one of five **authoring strata** (Section 6): engineered (S1), code-class UDA (S2), platform-constrained (S3), document-embedded/EUC (S4), and agentic (S5). Four rules govern the axis:
- **Objectives are invariant; expressions are stratified.** All control objectives apply to every stratum; what varies is the *expression* — the mechanism and form of evidence by which the objective is met. An independent logic review with input/output reconciliation expresses CO-15 in a spreadsheet exactly as automated regression gates express it in a pipeline.
- **Eligibility ceilings, never waived objectives.** Where a stratum's maximum achievable assurance falls below what the tier demands, the objective is not relaxed — the stratum is **ineligible at that tier** (Section 6 eligibility matrix). The asset migrates strata, or (legacy stock only, Section 7) operates under time-bound compensating controls with explicit risk acceptance.
- **Migration is the control of last resort and the first ambition.** Each stratum carries graduation triggers, and the organisation makes the next rung cheap: a lightweight paved road that makes S2→S1 adoption near-zero-cost is itself a control.
- **Agentic artifacts are code.** Prompts, system instructions, skills, tool and connector definitions, and evaluation suites are software artifacts subject to this standard in full (CO-12) — identically for professionally- and user-developed agents. An agent's tier derives from the **union of its tool grants**, not its stated purpose.

## 4. Interfaces to sibling standards (where this standard stops)

- **Model Risk Management.** This standard owns the *engineering* of AI/ML systems — how the code, data plumbing, pipelines, and inference services are built and secured, and the integrity of the model artifact as a supply-chain object. MRM owns model *validation, performance, fairness/bias, explainability, and behavioural monitoring*. **Seam:** the SDLC must emit the lineage and evaluation artifacts MRM requires (versioned model + data lineage, eval harness running in CI), and must block promotion of an in-scope model-bearing system to production without MRM disposition.
- **Change & Release Management.** This standard governs how a change is built and *technically* deployed; the enterprise change standard governs approval authority, risk acceptance, scheduling, and stakeholder communication. **Seam:** changes that demonstrably conform to an approved pipeline pattern are pre-authorised as standard changes — the pipeline record *is* the change record.
- **Third-Party / Supplier Risk.** TPRM owns supplier due diligence and contractual controls. This standard owns the technical supply-chain integrity controls for any third-party or open-source software ingested into a build, and the integration-security controls for SaaS/COTS. Build-time controls (PW) do not apply to unmodified COTS/SaaS; configuration, integration, and data-egress controls do.
- **IAM / Cloud / Vulnerability Management / Data.** Workload identity, secrets infrastructure, network and runtime hardening, vulnerability SLAs, and data classification are *consumed* by this standard but *defined* in those standards. Control statements here reference, not redefine, them.
- **Sector-Specific Runtime Controls (prescriptive regulatory regimes).** Owns the *runtime* control framework mandated by a prescriptive sector regime where one applies — for example, under MiFID II RTS 6: immediate order-cancellation ("kill") functionality, pre- and post-trade limits, real-time monitoring/surveillance, and the RTS 6 governance and annual self-assessment regime; analogous runtime obligations arise under IEC 62304 (medical devices), ISO 26262 (automotive), and DO-178C (aviation). **Seam:** this SDLC standard delivers the development, testing, controlled-deployment, change-authorisation, and record-keeping obligations such regimes place on the build lifecycle (CO-19) and ensures in-scope systems are engineered to expose and support those runtime controls; the sector-specific standard specifies and governs the runtime controls in operation.
- **Enterprise AI Policy & Standard.** The AI standard establishes and governs the AI asset taxonomy — **models, platforms, solutions, and features** — and owns use-case authorisation, the approved model/tool registry, responsible-AI requirements, and runtime guardrail policy. This standard owns the **engineering lifecycle of the artifacts** that compose AI solutions and agentic systems: how prompts, skills, tool/connector definitions, evaluation suites, and orchestration code are versioned, reviewed, tested, promoted, and rolled back (CO-12). **Seam (double-key):** AI-standard disposition gates *existence and use* — no in-scope solution is promoted to production, or has its use materially changed, without it; this standard gates *change* — every modification to an agentic artifact passes SDLC change control, without re-authorisation of the use case unless tool scope or use changes. **Agency is a cross-cutting attribute, not a category:** the capability to act through tools may attach to a model, a platform, a solution, or a vendor **feature**; the agency flag, CO-12 controls, and scope-derived tiering apply wherever it attaches. Feature-class agency arriving in vendor software is assessed at vendor-change through the Third-Party seam. User-built agents with persistent prompt, tool grants, and trigger are **solutions**, however small — inventoried, tiered, and ceiling-enforced by the platform.

## 5. Criticality tiering (proportionality basis)

All control statements are mandatory **by tier**. Tier is the higher of operational criticality, data sensitivity, and change blast-radius/reversibility.

| Tier | Definition | Examples |
|---|---|---|
| **Tier 1 — Critical** | Supports a critical operation; failure breaches an operational-resilience tolerance or affects financial integrity, safety, or systemic stability | Safety-critical operations, systems of financial or data integrity, customer-facing services, industrial or medical control systems, widely-consumed open-source components |
| **Tier 2 — Important** | Material to a business service but failure is tolerable for a bounded period; handles Confidential data | Internal workflow systems, reporting platforms, supporting microservices |
| **Tier 3 — Standard** | Limited blast radius; no Confidential data of consequence | Internal tools, low-risk automation, prototypes |

Where a control statement is tier-conditional, the minimum applicable tier is noted as **[T1]**, **[T1–2]**, or **[All]**.

## 6. Authoring strata and eligibility (proportionality, second axis)

Strata are cut by **control surface, not author** — an analyst's Python and an engineer's Python face the same control possibilities; authorship is an attribute under CO-1/CO-3, not a stratum.

| Stratum | Control surface | Canonical examples |
|---|---|---|
| **S1 Engineered** | Full toolchain: VCS, CI/CD pipeline, automated gates | Professional SDLC; user-authored code that adopts the paved road |
| **S2 Code-class UDA** | Versionable and testable with modest uplift | Analyst / data-science Python, R, SQL, notebooks, scripts outside the professional toolchain |
| **S3 Platform-constrained** | The platform's: segregated environments, governed connectors, promotion gates | Low-code/no-code apps; governed BI with promotion gates |
| **S4 Document-embedded / EUC** | Minimal: logic and data entangled; no VCS semantics, no test harness | Spreadsheets, Access databases, macros/Office Scripts, artifact-embedded BI |
| **S5 Agentic** | Configuration-as-code + non-deterministic behaviour + tool-scoped blast radius | Agents, prompts, skills, connectors/MCP configs, scheduled AI automations — professional and user-developed |

**Eligibility matrix.** Tier (Section 5) sets control *intensity*; stratum sets control *expression*; this matrix is where they meet. "Native" = the stratum's standard expression suffices; "uplift" = the stratum's elevated expression set applies; "prohibited (new)" binds new builds from commencement, with legacy stock governed under Section 7.

| Stratum | Tier 1 | Tier 2 | Tier 3 | Tier 4 |
|---|---|---|---|---|
| **S1** | Native | Native | Native | Native |
| **S2** | Ineligible as S2 — **S1 expression required** (adopt the paved road) | Eligible with S2 uplift | Native | Native |
| **S3** | Eligible **only under professional governance** — the platform is a toolchain choice, not a governance relief | Eligible with S3 expression under professional oversight | Native | Native |
| **S4** | **Prohibited (new)**; legacy via §7 pipeline | **Prohibited (new)**; legacy via compensating set + migration clock | Eligible with the EUC compensating set (CO-13.5) | Native |
| **S5 — professional** | Eligible with full Tier-1 agentic expression incl. human gate (CO-12.6) | Eligible | Eligible | Eligible |
| **S5 — user-developed** | Prohibited | Prohibited — graduate to professional governance | Eligible on the governed agent platform; reversible/read-only tool scopes by default | Eligible on the governed agent platform |

## 7. Transitional provisions — commencement, flow and stock

- **7.1 Flow binds at commencement.** All controls, prohibitions, and eligibility ceilings apply to net-new assets from the standard's effective date. The **preventative mechanisms must be operational at commencement** — platform registration-at-creation, default-deny high-risk connectors, the lightweight S2 paved road, and tenant-level discovery scanning — because a prohibition without prevention manufactures undiscovered debt.
- **7.2 Date-stamp rule.** Assets created on or after commencement outside the required controls are **non-compliant** (exception or breach under CO-1.3) — never "legacy". Pre-commencement assets enter the remediation portfolio. This distinction prevents the stock pipeline becoming an amnesty for new debt.
- **7.3 Stock pipeline.** Commencement triggers **discovery → triage → disposition** for the existing estate: discovery via tenant scanning (file stores, macro inventories, BI estates, automation/agent inventories); triage to tier × stratum, highest-consequence first; disposition to one of **migrate / retire / remediate-with-compensating-controls / formally accept**. Clocks *(calibrate)*: discovery complete within [n] months; Tier-1/2-consequence assets dispositioned within [n]; full estate within [n].
- **7.4 Debt governance.** The remediation portfolio is managed as a **tier-weighted backlog** under CO-17/CO-18, with burn-down, discovery-rate trend, and — the headline metric — **new-debt-creation rate, which must trend to zero**, reported to technology-risk governance.

---

# Control objectives and control statements

> Each objective is expressed as a control intent, followed by auditable control statements (imperative, testable), and a framework mapping line for independent-risk-oversight and independent-assurance traceability.

## A. Foundational

### CO-1 · Governance, ownership and risk tiering
**Intent:** Every system has accountable ownership and a risk tier that drives control intensity; the standard is enforced, not advisory.

- **CO-1.1 [All]** Every software asset shall have a single accountable owner (the accountable first-line owner) recorded in the organisation's application register, and shall be assigned a criticality tier per Section 5 at inception and re-validated on material change.
- **CO-1.2 [All]** Compliance with this standard shall be demonstrable from system-generated evidence (Section CO-14), not attestation alone.
- **CO-1.3 [All]** Any deviation from a mandatory control statement shall be governed by a time-bound, risk-accepted exception with a remediation plan; Tier-1 exceptions require independent risk-oversight review and named senior-executive acceptance.
- **CO-1.4 [T1–2]** Control coverage and exception posture shall be reported to technology-risk governance on a defined cadence, with thematic trends (not just counts) surfaced to the relevant risk committee.
- **CO-1.5 [All]** This standard shall be governed as a controlled management-system artifact — version-controlled, reviewed on a defined cadence, and subject to periodic independent assurance of its operation, with findings driving corrective action and improvement (ISO 9001 principles). Organisational maturity in applying the standard shall be assessed against an approved maturity model — CMMI for process maturity, BSIMM / OWASP SAMM for software-security-program maturity — with results informing the improvement roadmap.

*Mapping: CSF GV.OC, GV.RR, GV.RM, GV.OV · SSDF PO.1, PO.2 · ISO 9001 (management review, internal audit) · CMMI / BSIMM / SAMM (maturity)*

### CO-2 · Secure-by-design and threat modelling
**Intent:** Security and resilience requirements are defined up front and architecture is steered onto safe defaults.

- **CO-2.1 [All]** Security, privacy, and resilience requirements shall be defined as part of the requirements baseline for every new system or material change, derived from data classification and tier.
- **CO-2.2 [T1–2]** A threat model shall be produced and maintained for the system, proportionate to tier, and re-assessed on material architectural change; identified threats shall be tracked to mitigation or accepted risk.
- **CO-2.3 [All]** Engineering shall default to organisation-approved **paved-road** reference architectures, patterns, and platforms; divergence from a paved road requires documented architectural justification and elevated assurance.
- **CO-2.4 [T1]** Designs supporting critical operations shall demonstrate alignment to the operational-resilience tolerances for the operations they support (recoverability, failover, dependency concentration).
- **CO-2.5 [All]** The organisation shall maintain a governed set of approved **delivery-model paved roads** (e.g. cloud-native CI/CD, legacy/COTS release, low-code), each a pre-accredited implementation of this standard's control objectives with its reference toolchain and control wiring. Teams shall adopt an approved delivery model; use of an unapproved model, or material divergence from a paved road, requires documented justification and elevated assurance (consistent with CO-2.3). Controls are not differentiated by lifecycle methodology; differentiation is by criticality tier (Section 5).

*Mapping: CSF ID.RA, PR.IP, GV.SC · SSDF PW.1, PO.5 · ISO/IEC 12207 (lifecycle processes)*

## B. Build-time

### CO-3 · Developer enablement and competency (prepare the organisation)
**Intent:** People and toolchains are equipped to build securely by default.

- **CO-3.1 [All]** Personnel and automated identities with the ability to author or change code shall be authorised through role-based access, with capability granted on least-privilege and reviewed periodically.
- **CO-3.2 [All]** The organisation shall provide approved, centrally-managed toolchains (SCM, build, test, deploy, secrets) with secure defaults; use of unapproved development tooling for the organisation's software is prohibited.
- **CO-3.3 [All]** Engineers, and the owners of UDA and AI-assisted workflows, shall complete role-appropriate secure-development training before being granted build capability and on a recurring basis.

*Mapping: CSF PR.AT, GV.RR · SSDF PO.2, PO.3*

### CO-4 · Source code integrity and change provenance
**Intent:** What changed, by whom or what, and on whose authority is provable and tamper-evident.

- **CO-4.1 [All]** All source, build definitions, and infrastructure/policy code shall reside in approved, access-controlled version control; the canonical history shall be protected against rewriting.
- **CO-4.2 [T1–2]** Commits to protected branches shall be cryptographically signed and attributable to a verified identity (human or workload).
- **CO-4.3 [All]** Every change merged to a release branch shall undergo recorded peer review by an independent reviewer; **review applies identically to AI-generated and human-authored code — there is no review exemption** (see CO-12).
- **CO-4.4 [T1]** No single human or automated identity shall be able to author, approve, *and* deploy a change to a Tier-1 system unaided. Segregation of duties shall be enforced **technically in the pipeline** (branch protection, required independent approval, protected deployment gates), not by procedure alone.

*Mapping: CSF PR.AC, PR.DS, DE.CM · SSDF PS.1, PS.2, PW.7*

### CO-5 · Software supply chain integrity
**Intent:** Every dependency and build input — open-source, third-party, AI-generated, or the build system itself — is governed, inventoried, and provably untampered.

- **CO-5.1 [All]** Third-party and open-source components shall be sourced only from approved, integrity-verified registries/mirrors; components shall be screened for vulnerabilities, licence compatibility, and maintenance/provenance risk before adoption.
- **CO-5.2 [All]** A complete, machine-readable **SBOM** (SPDX or CycloneDX) shall be generated at build time for every release artifact, stored as an immutable attestation bound to the artifact digest, and continuously evaluated against vulnerability and licence-policy feeds — including transitive dependencies.
- **CO-5.3 [T1–2]** All artifacts deployed to production shall carry verifiable **provenance attestation (minimum SLSA Build Level 3)**: produced by a hardened, isolated, ephemeral build service from version-controlled source, with the *build platform* — not the developer — generating signed provenance.
- **CO-5.4 [T1–2]** Deployable artifacts (packages, container images, IaC bundles, model artifacts) shall be cryptographically signed; runtime/admission control shall verify signature and provenance and **reject unsigned or unattested artifacts**.
- **CO-5.5 [All]** Dependencies shall be version-pinned/locked; automated, tested dependency-update flows shall be in place to remediate vulnerable components within the applicable SLA (CO-11).

*Mapping: CSF GV.SC, ID.RA, PR.DS · SSDF PO.1, PS.3, PW.4, RV.1*

### CO-6 · Secure build and CI/CD pipeline integrity
**Intent:** The pipeline is hardened, isolated, and tamper-evident, because it is now the highest-value attack surface and the source of truth for control evidence.

- **CO-6.1 [All]** Pipelines shall be defined as version-controlled code and subject to the same review and change controls as application code.
- **CO-6.2 [T1–2]** Builds shall execute in **ephemeral, isolated, least-privilege** environments with no inbound interactive access and no persistence of secrets or state between runs.
- **CO-6.3 [All]** Access to pipeline configuration, signing keys, and deployment credentials shall be role-restricted and logged; the pipeline shall not run with broad standing privilege.
- **CO-6.4 [T1]** Promotion to a Tier-1 production environment shall require a protected approval gate enforcing independent human authorisation distinct from the change author (reconciling DevOps velocity with SoD per CO-4.4).
- **CO-6.5 [All]** Pipeline execution shall emit tamper-evident logs sufficient to reconstruct what was built, from which source revision, by which identity, with which controls passed.

*Mapping: CSF PR.AC, PR.PT, DE.CM, PR.IP · SSDF PS.1, PW.6, PO.3*

### CO-7 · Continuous security testing and assurance
**Intent:** Defects and exposures are detected automatically on every change and gated proportionate to tier.

- **CO-7.1 [All]** The pipeline shall run, as automated gates, the assurance checks appropriate to the artifact: SAST, software composition analysis (SCA), secrets scanning, IaC/configuration scanning, and container/image scanning.
- **CO-7.2 [T1–2]** Dynamic and/or interactive testing (DAST/IAST) shall be performed against running services proportionate to tier and exposure prior to production release.
- **CO-7.3 [All]** Risk-based gating thresholds shall be defined by tier and severity; findings above threshold shall **block** progression unless covered by an approved exception (CO-1.3).
- **CO-7.4 [T1]** Independent security testing (e.g. penetration testing / red-team) shall be performed on Tier-1 systems prior to first production release and on a risk-based recurring basis thereafter.
- **CO-7.5 [All]** Test data shall not contain unmasked production-sensitive data; data used in non-production shall be de-identified or synthetic per the Data standard.

*Mapping: CSF ID.RA, PR.IP, DE.CM, PR.DS · SSDF PW.5, PW.7, PW.8, RV.1*

### CO-8 · Secrets, keys and non-human identity
**Intent:** Credentials are never embedded, always short-lived, and machine identity is governed as rigorously as human identity.

- **CO-8.1 [All]** No credential, key, token, or other secret shall be committed to source control or embedded in build artifacts, images, or IaC. Pre-commit and pipeline secret scanning shall **block on detection**.
- **CO-8.2 [All]** Any exposed secret shall be treated as compromised: revoked, rotated, and handled under the incident process.
- **CO-8.3 [All]** Secrets shall be retrieved at runtime from an approved secrets-management service; static long-lived secrets are prohibited for Tier-1 and Tier-2 systems.
- **CO-8.4 [T1–2]** Service-to-service authentication shall use short-lived, automatically-rotated **workload identity** (e.g. federated workload credentials); standing static service credentials are prohibited.

*Mapping: CSF PR.AC, PR.DS · SSDF PW.9, PS.2 (consumes IAM standard)*

### CO-9 · Infrastructure and configuration as code
**Intent:** Environments are reproducible, governed, and free of unmanaged drift.

- **CO-9.1 [T1–2]** Production infrastructure and configuration shall be defined and provisioned as version-controlled code; manual, out-of-band changes to Tier-1 environments are prohibited except under break-glass with mandatory post-event reconciliation.
- **CO-9.2 [All]** Policy-as-code guardrails shall enforce security and compliance baselines (network exposure, encryption, identity, region/data-residency) at provisioning time, failing non-compliant deployments closed.
- **CO-9.3 [T1–2]** Configuration drift shall be continuously detected against the declared state and remediated or reconciled; immutable-infrastructure patterns are preferred for Tier-1.

*Mapping: CSF PR.IP, PR.PT, DE.CM, ID.AM · SSDF PW.6, PO.5 (consumes Cloud/Infra standard)*

### CO-15 · Functional quality assurance and test strategy
**Intent:** Software is demonstrably correct and fit for purpose; testing is risk-based, automated, and gates change — the correctness counterpart to the security testing in CO-7.

- **CO-15.1 [All]** A documented, risk-based test strategy shall define the required test types and depth by tier — unit, integration, regression, system, and user-acceptance — proportionate to consequence.
- **CO-15.2 [All]** Functional tests shall be automated and executed in the pipeline as a gate; changes failing required tests shall not progress.
- **CO-15.3 [T1–2]** An automated **regression suite** shall be maintained and run on every change to guard against reintroduced defects; regression coverage of critical paths shall be a release gate.
- **CO-15.4 [All]** Acceptance criteria / definition-of-done shall be defined per change, with traceability from requirement → test case → result (extends CO-14.2).
- **CO-15.5 [T1]** Material changes to Tier-1 systems shall undergo formal user/business acceptance with recorded sign-off before production release.
- **CO-15.6 [All]** Testing shall use environments with managed parity to production for the dimensions under test, and de-identified or synthetic data (links CO-7.5, CO-9).

*Mapping: CSF ID.RA, PR.IP · SSDF PW.7, PW.8 · ISO/IEC 25010 (functional suitability, reliability) · ISO/IEC/IEEE 12207*

### CO-16 · Non-functional quality: performance, capacity, resilience and operability
**Intent:** Software meets its performance, scalability, resilience, and supportability requirements — the dimensions behind most severe production incidents.

- **CO-16.1 [T1–2]** Non-functional requirements (performance, throughput, latency, capacity, scalability, availability, recoverability) shall be defined for the system and traced to test.
- **CO-16.2 [T1–2]** Performance and capacity testing shall be conducted against defined NFRs prior to production release and on material change; threshold breaches shall gate release.
- **CO-16.3 [T1]** Resilience shall be validated through fault-injection / failure testing (e.g. chaos or DR testing) proportionate to the operational-resilience tolerances of the operations supported (links CO-2.4, CO-10.2).
- **CO-16.4 [All]** Systems shall be **observable by design**: logging, metrics, and tracing sufficient to detect, diagnose, and alert on failure shall be built in and validated before release.
- **CO-16.5 [T1–2]** A production-readiness / operational-acceptance review shall confirm monitoring, alerting, runbooks, capacity headroom, and supportability before a system enters or materially changes in production.

*Mapping: CSF ID.BE, PR.PT, DE.CM, RC.RP · ISO/IEC 25010 (performance efficiency, reliability, maintainability) · ITIL 4 (service validation, monitoring)*

### CO-17 · Code quality, maintainability and technical debt
**Intent:** The codebase stays correct, maintainable, and measurable; structural quality is the leading indicator of future defect and change risk.

- **CO-17.1 [All]** Organisation-approved coding standards shall be defined per language/platform and enforced automatically (linting, static quality analysis) in the pipeline.
- **CO-17.2 [T1–2]** Structural quality shall be measured against defined thresholds — e.g. ISO/IEC 5055 / CISQ measures for reliability, maintainability, and performance efficiency; cyclomatic complexity; duplication; test coverage — with breaches tracked and gated by tier.
- **CO-17.3 [All]** Technical debt shall be identified, recorded, and managed as a tracked risk prioritised by consequence; debt in Tier-1 systems shall be visible to system governance.
- **CO-17.4 [T1–2]** Material systems shall maintain current design and operational documentation sufficient to support, change, and recover the system independently of individual personnel (maintainability / key-person-risk control).

*Mapping: CSF GV.RM, PR.IP, ID.RA · SSDF PW.1, PW.2 · ISO/IEC 25010 (maintainability) · ISO/IEC 5055 / CISQ*

## C. Release and run

### CO-10 · Deployment safety and operational resilience
**Intent:** Change — the leading driver of operational incidents — is engineered to fail safely and recover within tolerance.

- **CO-10.1 [T1–2]** Changes to systems supporting critical operations shall be deployed via **progressive techniques** (canary or staged rollout) with **automated, health-based rollback**.
- **CO-10.2 [T1]** Deployment design shall demonstrate the ability to restore service within the operation's defined disruption tolerance, including tested rollback/forward-fix paths.
- **CO-10.3 [All]** Deployment shall be separated from release where feasible (e.g. feature flags) so that exposure can be controlled and reversed independently of code shipment.
- **CO-10.4 [All]** Every production deployment shall be traceable to its source revision, approvals, assurance evidence, and accountable deployer (links to CO-14).

*Mapping: CSF PR.IP, RC.RP, RS.MI, ID.BE · SSDF PW.6 (interfaces Change & Resilience standards)*

### CO-11 · Vulnerability response and post-release management
**Intent:** Software remains secure after release; exposures are found and fixed on a clock, and end-of-life is governed.

- **CO-11.1 [All]** SBOM and asset inventories shall be continuously evaluated against threat intelligence so that newly-disclosed vulnerabilities in deployed components are identified and routed for action.
- **CO-11.2 [All]** Remediation SLAs shall be defined by severity and tier; Tier-1 critical exposures shall be remediated or mitigated on an expedited, defined timeline, with overruns escalated.
- **CO-11.3 [T1–2]** A coordinated vulnerability-disclosure path shall exist for externally-reported issues, with defined triage and response.
- **CO-11.4 [All]** Decommissioning shall be a controlled state: data disposition, credential/identity revocation, dependency removal, and register update shall be completed and evidenced (hand-off to the Technology Lifecycle Standard).

*Mapping: CSF ID.RA, DE.CM, RS.MI, RC.RP · SSDF RV.1, RV.2, RV.3*

### CO-18 · Defect, problem and continuous improvement
**Intent:** Defects and incidents are systematically tracked, root-caused, and fed back into the lifecycle so the same change-and-process failures don't recur — the learning loop the quality and service-management frameworks require.

- **CO-18.1 [All]** Defects shall be tracked through to resolution with severity classification; **defect escape** (defects reaching production) shall be measured and trended by tier.
- **CO-18.2 [T1–2]** Production incidents with a software, change, or configuration root cause shall undergo structured root-cause analysis (problem management); corrective and preventive actions shall be tracked to closure and **fed back into the relevant control** — test coverage, requirements, NFRs, or deployment controls.
- **CO-18.3 [T1–2]** Software-delivery performance shall be measured using defined metrics — including **change-failure rate** and **time-to-restore** — and used to govern delivery-process health and target improvement. *(These are the DevOps "four keys"; distinct from the organisation's applicable operational-resilience regulation of the same acronym — e.g. the EU Digital Operational Resilience Act — referenced under resilience.)*
- **CO-18.4 [All]** Recurring or thematic root causes shall be escalated to technology-risk governance and shall drive updates to this standard, the paved roads, and the assurance gates.

*Mapping: CSF ID.IM, RS.AN, RC.IM, GV.OV · SSDF RV.2, RV.3 · ITIL 4 (problem management, continual improvement)*

## D. Modality overlays

> These extend the core objectives for specific development modalities. They modify, not replace, A–C.

### CO-12 · AI-assisted development and agentic systems (S5)
**Intent:** Code generated or completed by AI tools, and agentic systems themselves — professional or user-developed — are governed for security, provenance, IP contamination, behaviour, and accountability, with agentic configuration artifacts treated as code, and without creating an unmanaged fast lane around the core controls.

- **CO-12.1 [All]** Only organisation-approved AI coding assistants and agent platforms shall be used to produce the organisation's software; use of unapproved tools is prohibited (consistent with CO-3.2).
- **CO-12.2 [All]** Code generated or materially completed by an AI assistant shall be subject to the **same peer review, security testing, and provenance controls** as human-authored code, with no review or gating exemption (CO-4.3, CO-7).
- **CO-12.3 [T1–2]** Approved AI coding tools shall be configured to suppress verbatim reproduction of training data and to surface licence/attribution signals; outputs flagged as potential verbatim reproduction of restricted-licence code shall be **blocked from merge** pending review (IP-contamination control).
- **CO-12.4 [All]** Use of AI assistants shall be logged at the repository/change level sufficient to identify AI-influenced changes for assurance and incident purposes.
- **CO-12.5 [T1–2]** Autonomous agents able to commit code or trigger deployments shall operate under a **scoped, auditable workload identity with least privilege**; all agent actions shall be attributable and logged.
- **CO-12.6 [T1]** **No autonomous agent shall approve its own changes, nor deploy to a Tier-1 production environment, without explicit human authorisation.** Human-in-the-loop authorisation is mandatory at the Tier-1 production boundary.
- **CO-12.7 [All]** Where an AI assistant or agent is itself a hosted third-party service, the model, prompts/system instructions, and tool integrations shall be treated as supply-chain inputs governed under CO-5 and the Third-Party standard, and prompt-injection / tool-abuse risks shall be assessed for agentic workflows that act on the organisation's systems or data.
- **CO-12.8 [All]** **Agentic configuration artifacts are code.** Prompts, system instructions, skills, tool and connector definitions (incl. MCP configurations), memory/retrieval configurations, and evaluation suites shall be held in approved version control, independently reviewed, promoted through environments, attributable, and rollback-capable. A modification to any such artifact **is a change** under CO-4 and CO-10. This applies identically to professionally- and user-developed agents; production-editable prompts outside change control are prohibited.
- **CO-12.9 [All]** **Scope-derived tiering.** An agentic system's tier shall be assessed on CIAP impact over the **union of capabilities granted through its tools and the data reachable through them**, with the Section 5 irreversibility ratchet applied to agents executing irreversible external actions. Any expansion of tool scope is a material change triggering re-tiering and, where use changes, re-authorisation under the organisation's AI governance standard.
- **CO-12.10 [T1–2]** **Behavioural evaluation as the test suite.** A maintained evaluation suite shall execute in the pipeline and gate promotion of model, prompt, or tool changes, with statistical acceptance thresholds proportionate to tier (the agentic expression of CO-15); Tier-1 agents shall additionally undergo adversarial testing (prompt-injection, tool-abuse). Production behaviour shall be monitored against evaluation baselines, with material drift handled under CO-18.
- **CO-12.11 [All]** **User-developed agents (S5-user)** shall be built only on the organisation's governed agent platform, which shall enforce registration-at-creation, scoped per-agent workload identity, a centrally-governed connector allow-list defaulting to **reversible/read-only** scopes, and the Section 6 eligibility ceiling. Write access to systems of record requires graduation to professional governance. Tenant-level discovery shall operate for agentic automations created outside the platform.
- **CO-12.12 [All]** **AI-standard double-key.** In-scope AI solutions shall not be promoted to production, nor have their use materially changed, without current disposition under the organisation's AI governance standard; models and tools shall be consumed only from that standard's approved registry. SDLC change control (CO-12.8) governs all artifact modifications without re-opening use-case authorisation unless tool scope or use changes.

*Mapping: CSF GV.SC, PR.AC, ID.RA, DE.CM · SSDF SP 800-218A (PO/PS/PW for GenAI), PS.3, PW.4*

### CO-13 · User-developed applications (UDAs) — stratified governance (S2–S4)
**Intent:** Formalise rather than prohibit user development across the organisation's full estate — code-class artifacts, platform apps, and document-embedded/EUC solutions — with control expression set by stratum, tier eligibility enforced, and shadow IT prevented from reaching systems of record. (Agentic UDAs are governed under CO-12 as S5.)

- **CO-13.1 [All]** UDAs shall be **inventoried** in the enterprise application register with an accountable business owner and classified by tier (CO-1). For S4 and other populations with no creation chokepoint, inventory shall be **discovery-based** — periodic tenant scanning of file stores, macro estates, and BI artifacts — not registration-only, with discovered unregistered assets triaged under Section 7.
- **CO-13.2 [All]** Every UDA shall be **assigned an authoring stratum** (S2/S3/S4; S5 → CO-12) at registration or discovery, with the Section 6 **eligibility matrix enforced**: assets ineligible at their tier shall migrate strata, be retired, or (legacy stock only) operate under Section 7 compensating controls with explicit acceptance.
- **CO-13.3 [All] — S3 expression.** Low-code/no-code platforms shall enforce **segregated environments** with a governed promotion gate (direct authoring in production prohibited) and shall **prevent UDAs from connecting to Tier-1 systems of record or egressing Confidential data** without an exception approved by independent risk oversight; connectors and data flows are governed centrally. Platform updates are vendor changes assessed via the Third-Party seam.
- **CO-13.4 [All] — S2 expression.** Code-class UDAs shall reside in approved version control with peer review (independent review at Tier 2), dependencies sourced per CO-5, and blocking secret scanning (invariant, CO-8). The organisation shall provide a **lightweight paved road** making S2→S1 adoption near-zero-cost; at Tier 2 its use is the uplift requirement.
- **CO-13.5 [T3, legacy T2] — S4 expression (EUC compensating set).** Materially-consequential document-embedded UDAs shall carry, proportionate to tier: **independent logic review; input/output and reconciliation controls; master-copy version and access discipline** (protected master, change log); key-logic protection (locked/protected formulas); no embedded credentials (invariant, CO-8); macro execution restricted to signed/approved code; **periodic revalidation**; and documented purpose and owner.
- **CO-13.6 [All]** UDAs exceeding defined criticality, data-sensitivity, user-scale, or **complexity** thresholds shall be **graduated** to a higher stratum or into the professional SDLC (CO-2 through CO-11) rather than remaining under UDA governance; threshold breaches shall be detected from the CO-13.1 inventory, not self-declared.
- **CO-13.7 [All]** User developers shall be authorised, trained (CO-3.3), and their build capability reviewed; orphaned or unowned applications shall be quarantined or decommissioned.
- **CO-13.8 [All]** **New builds of S4 UDAs carrying Tier-1 or Tier-2 consequence are prohibited** from commencement; the pre-existing estate is governed exclusively through the Section 7 pipeline. Post-commencement creation outside these controls is a breach under the 7.2 date-stamp rule, not legacy.

*Mapping: CSF GV.OC, ID.AM, PR.AC, PR.DS, GV.SC · SSDF PO.1, PO.2, PS.1 · interfaces Section 6 (eligibility), Section 7 (transition), CO-12 (S5)*

### CO-19 · Regulated and high-consequence workloads
**Intent:** Workloads under prescriptive regulatory development regimes — IEC 62304 (medical device software), ISO 26262 (automotive), DO-178C (aviation), and MiFID II RTS 6 (algorithmic/electronic trading) among them — meet those obligations through the SDLC, with regime-mandated runtime controls owned by the applicable runtime-controls standard (Section 4 seam).

- **CO-19.1 [All applicable]** Workloads subject to prescriptive regulatory development obligations (e.g. IEC 62304 medical devices, ISO 26262 automotive, DO-178C aviation, MiFID II RTS 6 algorithmic trading) shall be identified and registered, and shall satisfy the applicable obligations through the controls in this standard, with conformance evidenced.
- **CO-19.2 [Where prescribed]** Regulated workloads shall be developed and tested under a documented methodology before deployment and after material change, including **conformance testing** with relevant counterparties, venues, or ecosystems and **testing against adverse or disorderly operating conditions** and stressed volumes where the regime prescribes it (e.g. RTS 6 trading-venue conformance testing and disorderly-market/stressed-message-volume testing), in environments segregated from production (extends CO-15, CO-16).
- **CO-19.3 [Where prescribed]** Deployment of regulated workloads shall be controlled and staged; material code and **parameter** changes shall be subject to recorded authorisation with a complete audit trail of who changed what and when (e.g. RTS 6 controls over algorithm deployment and parameter changes) (extends CO-4.4, CO-10).
- **CO-19.4 [Where prescribed]** Complete records of regulated systems, versions, parameters, and changes shall be retained to the regulatory requirement, and a **periodic self-assessment and validation** of the regulated systems and controls shall be performed and evidenced where the regime requires it (e.g. the RTS 6 annual self-assessment of algorithmic-trading systems and controls).
- **CO-19.5 [Interface]** Runtime controls mandated by a prescriptive regime — e.g. RTS 6's immediate order-cancellation ("kill") functionality, pre-/post-trade limits, and real-time surveillance — are owned and specified by the applicable **runtime-controls standard** (for trading, an Algorithmic / Electronic Trading Controls standard); this standard requires only that systems are *built to expose and support* those controls and that their presence is validated under CO-15/CO-16.

*Mapping: CSF GV.OC, GV.SC, PR.IP, DE.CM · prescriptive regimes e.g. IEC 62304, ISO 26262, DO-178C, MiFID II RTS 6 (Reg. (EU) 2017/589) · interfaces the applicable runtime-controls standard (e.g. Algorithmic/Electronic Trading Controls)*

## E. Cross-cutting

### CO-14 · Evidence, auditability and records
**Intent:** Every control produces immutable, attestable evidence, and any change is traceable end-to-end — the precondition for both internal assurance and regulatory examination.

- **CO-14.1 [All]** Control execution (reviews, scans and outcomes, approvals, signatures, provenance, deployments) shall generate **immutable, time-stamped, attributable evidence** retained per the records-management schedule.
- **CO-14.2 [T1–2]** End-to-end **traceability** shall exist from requirement → source revision → assurance results → approval → deployed artifact → running instance.
- **CO-14.3 [All]** Evidence shall be retrievable on demand to demonstrate control operation to independent risk oversight, independent assurance, and regulators without manual reconstruction.

*Mapping: CSF GV.OV, DE.CM, ID.GV · SSDF PO.3, PS.1, RV.3*

---

## Appendix — objective-to-framework coverage (summary)

| # | Control objective | CSF 2.0 | SSDF |
|---|---|---|---|
| CO-1 | Governance, ownership, tiering | GV | PO |
| CO-2 | Secure-by-design & threat modelling | ID, PR, GV | PW, PO |
| CO-3 | Developer enablement & competency | PR, GV | PO |
| CO-4 | Source integrity & change provenance | PR, DE | PS, PW |
| CO-5 | Software supply chain integrity | GV, ID, PR | PO, PS, PW, RV |
| CO-6 | Build & CI/CD pipeline integrity | PR, DE | PS, PW, PO |
| CO-7 | Continuous security testing | ID, PR, DE | PW, RV |
| CO-8 | Secrets, keys, non-human identity | PR | PW, PS |
| CO-9 | Infrastructure & config as code | PR, DE, ID | PW, PO |
| CO-10 | Deployment safety & resilience | PR, RC, RS, ID | PW |
| CO-11 | Vulnerability response & post-release | ID, DE, RS, RC | RV |
| CO-12 | AI-assisted development & agentic systems (S5) | GV, PR, ID, DE | 800-218A, PS, PW |
| CO-13 | UDAs — stratified governance (S2–S4) | GV, ID, PR | PO, PS |
| CO-14 | Evidence, auditability & records | GV, DE, ID | PO, PS, RV |
| CO-15 | Functional QA & test strategy | ID, PR | PW |
| CO-16 | Non-functional quality (perf/capacity/resilience/operability) | ID, PR, DE, RC | PW |
| CO-17 | Code quality, maintainability & technical debt | GV, PR, ID | PW |
| CO-18 | Defect, problem & continuous improvement | ID, RS, RC, GV | RV |
| CO-19 | Regulated & high-consequence workloads | GV, PR, DE | — (RTS 6) |
