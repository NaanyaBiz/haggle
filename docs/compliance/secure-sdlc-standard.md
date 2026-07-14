# Secure Software Development Lifecycle (SDLC) Standard

*An industry-agnostic, consequence-first standard for software however authored — human, AI-assisted, agentic, or end-user.*
*v1.0 — July 2026 · © 2026 NaanyaBiz — published under this repository's licence.*
*This repository operates under this standard; the statement-level conformance record is [conformance.md](./conformance.md).*

---

## 1. Purpose and scope

This standard sets the mandatory control objectives and control statements governing how software is designed, built, changed, deployed, and retired. It applies to **all software the organisation creates or materially modifies**, irrespective of who or what authored it — internal engineers, AI coding assistants, autonomous agents, or user developers — whose user-developed applications (UDAs) span code-class artifacts (e.g. analyst / data-science Python, R, SQL), low-code/no-code platform apps, and document-embedded solutions such as spreadsheets and kindred office-productivity artifacts (Section 6) — and irrespective of the runtime (on-prem, cloud, container, serverless, edge). It is written to scale by consequence, not by industry or size: the same objectives apply, at proportionate intensity, from a single-maintainer open-source project to a large regulated enterprise. Agentic systems and their configuration artifacts (prompts, skills, tool and connector definitions) are in scope as software (CO-12).

It does **not** restate controls owned by sibling standards; it defines the seams (Section 4).

**Interpretation — role terms are functions, not organisational structures.** Every role named in this standard — *accountable owner*, *independent risk oversight*, *independent assurance*, *technology-risk governance*, *the risk-governance function* — denotes a **function** (a separation of accountability), not a department, committee, or reporting line. In a large organisation these functions are typically held by distinct bodies; in a minimal one they may all collapse into the named accountable owner, who then carries the corresponding accountability personally. First-line risk accountability always sits with the accountable owner: acceptances are owner-signed. Where a statement requires a function *independent of* the owner and no such separation exists, the statement is not reinterpreted — it is governed as a recorded exception (CO-1.3) and revisited when the separation becomes possible.

**This copy is a tailored adoption.** This edition is adopted by, and tailored to, this repository: single accountable owner-maintainer; authoring stratum S1 with the S5 agentic overlay; declared Tier 3; no UDA estate; no prescriptive regulatory regime. Sections and statements whose governed population has no referent here are **compacted to indexed stubs** — heading, intent, and one line per statement, numbering preserved — per the Tailoring record at the end of this document. **Compaction is not waiver**: a compacted statement binds in full from the moment its population appears.

## 2. Position in the standards hierarchy

In an enterprise this standard sits under a Technology Lifecycle Standard (or equivalent policy root) beside sibling standards for change & release management, cloud & infrastructure, identity & access, vulnerability & threat management, third-party/supplier risk, data management, and model risk. That hierarchy is a **reference architecture**, not a mandated document count: each Section 4 seam needs an answer, however lightweight. *(Compacted — in this adoption the siblings collapse into the repository's committed policy artifacts; see Section 4.)*

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

The full edition defines six seams: model risk management, change & release management, third-party/supplier risk, IAM/cloud/vulnerability/data, sector-specific runtime controls, and AI governance. *(Compacted — in this adoption they collapse into this repository's committed artifacts:)* change control **is** the PR + ruleset flow; third-party and supply-chain integrity is CO-5's dependency gating; identity, secrets, and vulnerability response are CO-8/CO-11; data classification and handling resolve to SECURITY.md's data-handling posture and the diagnostics privacy contract (docs/diagnostics.md); AI governance of the agent toolchain is AGENTS.md § AI toolchain together with CO-12 — sibling-standard names appearing in CO text (e.g. "the Third-Party standard", "the organisation's AI governance standard") resolve to those artifacts. No model-risk or sector runtime-control seam has a referent here.

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
| **S5 Agentic** | Configuration-as-code + non-deterministic behaviour + tool-scoped blast radius | Agents, prompts, skills, connectors/MCP configs, scheduled AI automations — professional and user-developed |

*(Compacted — S2 code-class UDA, S3 platform-constrained, and S4 document-embedded/EUC are defined in the full edition; no referent in this adoption — see the Tailoring record and CO-13.)*

**Eligibility matrix.** Tier (Section 5) sets control *intensity*; stratum sets control *expression*; the matrix is where they meet. "Native" = the stratum's standard expression suffices. The binding rule: where a stratum's maximum achievable assurance falls below what the tier demands, the objective is not relaxed — the asset is **ineligible at that tier** and must migrate strata. Rows for strata with no referent here (S2–S4) are compacted (Tailoring record).

| Stratum | Tier 1 | Tier 2 | Tier 3 |
|---|---|---|---|
| **S1** | Native | Native | Native |
| **S5 — professional** | Eligible with full Tier-1 agentic expression incl. human gate (CO-12.6) | Eligible | Eligible |
| **S5 — user-developed** | Prohibited | Prohibited — graduate to professional governance | Eligible on the governed agent platform; reversible/read-only tool scopes by default |

## 7. Transitional provisions — commencement, flow and stock

- **7.1 Flow binds at commencement.** All controls, prohibitions, and eligibility ceilings apply to net-new assets from the standard's effective date, with the preventative mechanisms operational at commencement — a prohibition without prevention manufactures undiscovered debt. *(Compacted — the full edition enumerates the estate-scale mechanisms; this adoption has one governed repository.)*
- **7.2 Date-stamp rule.** Assets created on or after commencement outside the required controls are **non-compliant** (exception or breach under CO-1.3) — never "legacy". Pre-commencement assets enter the remediation portfolio. This distinction prevents the stock pipeline becoming an amnesty for new debt.
- **7.3 Stock pipeline** *(compacted — no pre-existing estate here)*: discovery → triage (tier × stratum, highest-consequence first) → disposition (**migrate / retire / remediate-with-compensating-controls / formally accept**), on defined clocks.
- **7.4 Debt governance** *(compacted)*: the remediation portfolio is a tier-weighted backlog under CO-17/CO-18; headline metric: **new-debt-creation rate trending to zero**, reported to technology-risk governance.

---

# Control objectives and control statements

> Each objective is expressed as a control intent, followed by auditable control statements (imperative, testable), and a framework mapping line for independent-risk-oversight and independent-assurance traceability.

## A. Foundational

### CO-1 · Governance, ownership and risk tiering
**Intent:** Every system has accountable ownership and a risk tier that drives control intensity; the standard is enforced, not advisory.

- **CO-1.1 [All]** Every software asset shall have a single accountable owner (the accountable first-line owner) recorded in the organisation's application register, and shall be assigned a criticality tier per Section 5 at inception and re-validated on material change.
- **CO-1.2 [All]** Compliance with this standard shall be demonstrable from system-generated evidence (Section CO-14), not attestation alone.
- **CO-1.3 [All]** Any deviation from a mandatory control statement shall be governed by a time-bound, risk-accepted exception with a remediation plan; Tier-1 exceptions require review by the independent risk-oversight function and recorded acceptance by the named accountable owner at the level of authority that owns the consequence (Interpretation, Section 1).
- **CO-1.4 [T1–2]** Control coverage and exception posture shall be reported to technology-risk governance on a defined cadence, with thematic trends (not just counts) surfaced to the risk-governance function, however constituted.
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
**Intent:** Formalise rather than prohibit user development — control expression set by stratum, tier eligibility enforced, shadow IT prevented from reaching systems of record. (Agentic UDAs are governed under CO-12 as S5.)

**Compacted — no UDA estate exists in this adoption** (Tailoring record; each statement binds in full if one appears):

- **CO-13.1 [All]** UDAs inventoried with an accountable owner and tier; discovery-based inventory for populations without a creation chokepoint.
- **CO-13.2 [All]** Every UDA assigned an authoring stratum, with the Section 6 eligibility ceiling enforced (migrate / retire / — legacy stock only — compensating controls with explicit acceptance).
- **CO-13.3 [All]** S3 platforms: segregated environments with a governed promotion gate; central connector/data-flow governance; no Tier-1 system-of-record connections or Confidential-data egress without an exception approved by independent risk oversight.
- **CO-13.4 [All]** S2 code-class UDAs: approved version control, review (independent at Tier 2), CO-5 dependency sourcing, blocking secret scanning; a lightweight paved road makes S2→S1 adoption near-zero-cost (at Tier 2 its use is the uplift requirement).
- **CO-13.5 [T3, legacy T2]** S4/EUC compensating set: independent logic review, input/output reconciliation, master-copy version and access discipline, protection of critical logic (locked formulas), no embedded secrets (invariant, CO-8), macro execution restricted to approved code, periodic revalidation, documented purpose and owner.
- **CO-13.6 [All]** UDAs exceeding criticality, data-sensitivity, user-scale, or complexity thresholds graduate to a higher stratum or the professional SDLC; breaches detected from inventory, not self-declared.
- **CO-13.7 [All]** User developers authorised, trained (CO-3.3), capability reviewed; orphaned applications quarantined or decommissioned.
- **CO-13.8 [All]** New S4 builds at Tier-1/2 consequence prohibited; pre-existing estate governed via Section 7; post-commencement creation outside controls is a breach (7.2), not legacy.

*Mapping: CSF GV.OC, ID.AM, PR.AC, PR.DS, GV.SC · SSDF PO.1, PO.2, PS.1 · interfaces Section 6 (eligibility), Section 7 (transition), CO-12 (S5)*

### CO-19 · Regulated and high-consequence workloads
**Intent:** Workloads under prescriptive regulatory development regimes (e.g. IEC 62304 medical devices, ISO 26262 automotive, DO-178C aviation, MiFID II RTS 6 algorithmic trading) meet those obligations through the SDLC; regime-mandated runtime controls are owned by the applicable runtime-controls standard (Section 4 seam).

**Compacted — no prescriptive regulatory regime applies to this adoption** (Tailoring record; binds in full if one ever does):

- **CO-19.1 [All applicable]** Regulated workloads identified and registered; applicable obligations satisfied through this standard, with conformance evidenced.
- **CO-19.2 [Where prescribed]** Documented development-and-testing methodology, including counterparty/venue/ecosystem conformance testing and testing against adverse or disorderly operating conditions and stressed volumes, in environments segregated from production (extends CO-15, CO-16).
- **CO-19.3 [Where prescribed]** Controlled, staged deployment; recorded authorisation and complete audit trail of material code **and parameter** changes (extends CO-4.4, CO-10).
- **CO-19.4 [Where prescribed]** Regulatory-grade records of systems, versions, parameters, and changes; periodic self-assessment and validation evidenced where the regime requires it.
- **CO-19.5 [Interface]** Regime-mandated runtime controls are owned and specified by the applicable runtime-controls standard; systems are built to expose and support them, with presence validated under CO-15/CO-16.

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

---

## Tailoring record (this adoption)

Tailored under the rule **compaction is not waiver**: a compacted section or
statement keeps its number, its intent, and its binding force — it is
compacted because its governed population has no referent in this repository,
and it re-binds in full the moment one appears. Reactivation = a PR restoring
the full text from the standard's master edition (held by the standard's
author).

| Compacted | Why (no referent) | Reactivation trigger |
|---|---|---|
| Section 2 hierarchy detail · Section 4 seam detail | Sibling standards collapse into this repository's committed artifacts | Adoption by a multi-standard organisation |
| Section 6 S2–S4 strata definitions + eligibility rows | Single S1 codebase + S5 agent artifacts | Any S2/S3/S4 artifact entering scope |
| Section 7.1/7.3/7.4 estate machinery | No pre-existing stock; one governed repository | Estate growth beyond this repository |
| CO-13 (8 statements) | No UDA estate | First UDA |
| CO-19 (5 statements) | No prescriptive regulatory regime | A regulated workload or regime applicability |

Erratum corrected against the master edition: the Section 6 eligibility
matrix carried an undefined **Tier 4** column (Section 5 defines three
tiers); the column is removed in this edition.
