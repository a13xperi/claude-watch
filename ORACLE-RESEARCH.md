# Oracle Research — Delphi OS

> Research conducted 2026-04-08. Searched Notion, GitHub, and all local repos for "Pierce Oracle" / Oracle infrastructure.

**Key finding:** "Pierce Oracle" has no matches anywhere. Two distinct Oracle systems exist under Delphi OS.

---

## 1. The Oracle (Atlas Onboarding) — SHIPPED

AI copilot guiding analysts through voice/tonality calibration during onboarding.

### Backend (~979 LOC)
| File | Purpose |
|---|---|
| `atlas-backend/services/api/src/routes/oracle.ts` (529 LOC) | Express routes: `/api/oracle/message`, `/chat`, `/agent` |
| `atlas-backend/services/api/src/lib/oracle-prompt.ts` (200 LOC) | System prompts, personality spec, calibration commentary |
| `atlas-backend/services/api/src/lib/oracle-tools.ts` (250 LOC) | 12 tool definitions for agent mode |

### Frontend (~1,766 LOC)
| File | Purpose |
|---|---|
| `atlas-portal/src/lib/oracle.ts` (184 LOC) | Core oracle logic |
| `atlas-portal/src/lib/oracle-agent.tsx` (260 LOC) | React agent component |
| `atlas-portal/src/lib/oracle-types.ts` (85 LOC) | Type definitions |
| `atlas-portal/src/lib/oracle-agent-types.ts` (56 LOC) | Agent types |
| `atlas-portal/src/lib/oracle-action-executor.ts` (199 LOC) | Action execution |
| `atlas-portal/src/lib/oracle-messages.ts` (160 LOC) | Message utilities |
| `atlas-portal/src/components/oracle/FloatingOracle.tsx` (260 LOC) | Floating widget |
| `atlas-portal/src/components/oracle/OracleWidget.tsx` (68 LOC) | Widget variant |
| `atlas-portal/src/components/onboarding/OracleChat.tsx` (633 LOC) | Chat UI |
| `atlas-portal/src/components/onboarding/OracleAvatar.tsx` (35 LOC) | Avatar |
| `atlas-portal/src/components/onboarding/OracleMessage.tsx` (86 LOC) | Message bubble |

### LLM Routing
- **Haiku** — acknowledgments, topic suggestions (fast)
- **Sonnet** — calibration commentary, blend previews (smart)
- **Opus** — complex draft generation

### Onboarding Flow
`WELCOME → TRACK_A/B → REFERENCES → BLEND → TOPICS → HANDOFF`

### Production Status
- 6 endpoints live
- 16 reference accounts
- Telegram parity (shared system prompt via `oracle-prompt.ts`)
- QA audit completed (session SL-098, 2026-04-04)

---

## 2. Oracle (Standalone Intelligence Platform) — SPEC'D, NOT BUILT

Separate product under Delphi OS. Private AI-native intelligence platform for Delphi Ventures GPs.

### Core Problem
Intelligence fragmented across Granola transcripts, email, Attio CRM, Google Drive. GPs need unified synthesis.

### Notion References
| Document | Page ID |
|---|---|
| Oracle Deployment Strategy | `33503ff6-a96d-81ff-b31f-c3c97c875a84` |
| Master Build Brief | `e2fdadcf-ccb2-4b93-83e6-320688206b23` |
| MC-004: Evaluate scope/roadmap | `33503ff6-a96d-8112-92c4-e11a035db13d` (📋 Backlog) |

### Planned Stack
| Layer | Technology |
|---|---|
| Frontend | `oracle-portal` — Next.js 15, shadcn/ui, Vercel |
| Backend | `oracle-api` — Python 3.12, FastAPI, Railway |
| Database | New Supabase project, PostgreSQL 16 + pgvector |
| Task queue | Celery + Redis |
| Extraction | Claude Sonnet 4 (bulk) |
| Synthesis | Claude Opus 4 |
| Embeddings | Voyage AI |
| Integrations | Attio CRM, Granola, Google Drive, Pythia/Telegram |

### Phased Deployment
| Phase | Scope | Est. Cost/mo |
|---|---|---|
| 0–2 | Foundation → Core UI → Intelligence | $375–1,000 |
| 3+ | Warm Path → Synthesis + Enrichment | $775–1,800 |

### Key Distinction: Oracle vs Atlas
- **Atlas** — Content/tweet crafting for analysts. Next.js + Express. Existing infra.
- **Oracle** — Investment intelligence for 5 GP users. Python/FastAPI. Separate infra, separate DB, separate frontend.

---

## 3. Oracle Personality Spec

- Mysterious but approachable
- DeFi-native voice
- Brief: 2–3 sentences max
- Hooded robot mascot with teal chevron eyes
- "RUNNING DELPHI OS" visor text
- 12 voice dimensions: humor, formality, brevity, contrarian tone, directness, warmth, technical depth, confidence, and more

---

## Git History (19 Oracle commits in atlas-backend)
| Hash | Description |
|---|---|
| `5a28402` | feat: model tiering (Haiku for chat, Opus for drafts) |
| `a6f4545` | feat: destructive Oracle tools + server-side refine |
| `eb56413` | feat: Oracle Agent endpoint with tool_use support |
| `1175924` | feat/oracle-chat-api (FloatingOracle widget) |
| `472a632` | feat: Oracle personality for Telegram bot |

---

## Open Questions
1. What is "Pierce Oracle"? No matches found — clarification needed from Alex.
2. MC-004 (standalone Oracle roadmap) is still in Backlog — when to start?
3. No repos created yet for `oracle-api` or `oracle-portal`.
