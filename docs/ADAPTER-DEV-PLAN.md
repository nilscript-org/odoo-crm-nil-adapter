# Odoo CRM Adapter — Development Plan (adapter-only)

**Date:** 2026-06-22 · **Scope:** the `odoo-crm-nil-adapter` repo ONLY. Zero kernel changes.
**Driver:** the evidence-based gap analysis (8 verbs today; non-create flows are inexpressible).
**Constraint:** bus-factor 1 → strict sequencing, one verb at a time, no parallel work.

---

## 0. Layer boundary (the rule that governs every item below)

- **Adapter = policy:** which verbs exist, which Odoo fields are writable, how risky *this* op is (tier),
  how to reverse it (compensation), how args map to Odoo (`to_native`).
- **Kernel = mechanism:** propose→commit, idempotency replay, compensation envelope shape.
- If a change needs `if system == "odoo"` in the kernel → it's a layer leak; redesign.

**Two items are NOT in this adapter plan (flagged, deferred to a kernel/contract task):**
1. **Read-path 404 on unknown verb** (§3 of the gap doc). The query handler should refuse with
   `UNKNOWN_VERB` like the write path does, not HTTP 404. This lives in the generic NIL edge and is a
   *contract-correctness* fix — and the edge is vendored in multiple adapters, so it must be synced
   across copies (the known multi-copy `HttpEventEmitter` drift gotcha). Do it as its own change.
2. **Atomic multi-entity bulk commit** (C2). True atomicity across N entities needs a kernel
   mechanism (atomic commit of a proposal group). The adapter can offer best-effort bulk now, but
   atomic bulk waits on the kernel.

---

## 1. The enabling refactor (do this FIRST — it unblocks half the gaps)

**Problem:** `edge.py` commit infers execution from the verb-name prefix:
`delete_*`→DELETE, `update_*`→UPDATE/PATCH, everything else→CREATE. That only models CRUD.
But most missing capabilities are **not CRUD**: `log_note` (chatter), `convert_lead`, `set_won/lost`,
`schedule_activity`, `archive` (soft-delete = write `active=False`), `upsert` (create-or-update).

**Fix (adapter-internal, no kernel touch):** give `WriteVerb` an explicit execution strategy instead
of inferring it from the name:

```python
class WriteVerb:
    op: Literal["create","update","delete","archive","upsert","method"]
    method: str | None          # for op="method": the Odoo model method, e.g. "message_post", "action_set_won"
    dedup_keys: tuple[str,...]   # for op="upsert": e.g. ("email_normalized","phone_sanitized")
    writable: tuple[str,...]     # whitelist for op="update" partial writes
    ...
```

`edge.py` commit then dispatches on `verb.op`, not the name prefix. Each `op` has one handler +
one compensation rule. This is the spine; every verb below plugs into it.

**Supporting I/O in `system.py` (`RealSystemClient` + the `FakeSystem` test double, kept in lockstep):**
- `search(model, domain, fields=…, limit=…)` — indexed lookup (phone/email), tag listing, dedup probe.
- `message_post(model, rid, body)` — chatter / log note.
- `call_method(model, method, ids, **kwargs)` — generic model-method dispatch (convert, won/lost, activity).
- `archive`/`unarchive` = `write(model, id, {"active": False/True})` (reuses `update`).

**Definition of done for the refactor:** all 8 existing verbs re-expressed via `op` with identical
behavior; full conformance matrix still green; no kernel diff.

---

## 2. Per-verb recipe (every verb is mechanical once §1 lands)

For each verb: (a) `WriteVerb`/`QueryVerb` entry in `translate.py` with `op`, `tier`, `to_native`,
`preview`; (b) `compensation.py` entry if reversible; (c) one conformance test (propose→commit→read-back
+ refusal cases); (d) `manifest.py` declaration. Tier is a **policy decision per verb** — destructive
or money-touching ⇒ HIGH (human-gated); routine writes ⇒ MEDIUM.

---

## 3. Phase 0 — SHIP-BLOCKERS (no launch without these)

The agent's WhatsApp entry point is a **phone number**; today it hits a wall on the first non-create turn.

| Verb | op | Odoo | tier | reversibility | notes |
|---|---|---|---|---|---|
| `crm.update_contact` | update | `res.partner.write` | MEDIUM | COMPENSABLE (restore `before`) | partial; **whitelist** writable fields (name, phone, email, comment, …); idempotency on contact_id+fields |
| `crm.get_contact_by_phone` | query | `res.partner.search` on `phone_sanitized` | — | — | indexed lookup; replaces the illegal full-scan via `list_contacts` on the hot path |
| `crm.create_contact` → **upsert** | upsert | search dedup on `email_normalized`/`phone_sanitized`, then create-or-update | MEDIUM | **conditional**: created→delete; matched+updated→restore `before` | prevents webhook-retry from duplicating the identity graph (the moat) |
| `crm.log_note` | method | `message_post(model, id, body)` | MEDIUM | IRREVERSIBLE (chatter is append-only) | builds the audit asset; target = any model+id |

**Phase 0 exit:** an agent can resolve a phone → contact, update it, idempotently upsert on inbound,
and leave an audit note — the minimum real WhatsApp CRM loop.

---

## 4. Phase 1 — make it a real CRM (B)

Order by damage-closed, not ease. **`archive_contact` first — it closes the IRREVERSIBLE hole** (today
the only removal is destructive HIGH delete; production needs reversible soft-delete).

| Verb | op | Odoo | tier | reversibility |
|---|---|---|---|---|
| `crm.archive_contact` | archive | `write active=False` | MEDIUM | COMPENSABLE → unarchive |
| `crm.update_lead` | update | `crm.lead.write` (whitelist: expected_revenue, priority, …) | MEDIUM | COMPENSABLE |
| `crm.schedule_activity` | method | `activity_schedule(...)` | MEDIUM | COMPENSABLE → unlink activity |
| `crm.convert_lead_to_opportunity` | method | lead convert action | MEDIUM | COMPENSABLE (best-effort) |
| `crm.set_lead_won` / `crm.set_lead_lost` | method | `action_set_won` / `action_set_lost` (+ lost_reason) | MEDIUM | COMPENSABLE → reset stage/probability |
| `crm.assign_lead` | update | write `user_id`/`team_id` | MEDIUM | COMPENSABLE |
| `crm.link_lead_contact` | update | write `partner_id` | MEDIUM | COMPENSABLE |
| `crm.add_tag` | update | write `tag_ids`/`category_id` (m2m command) | MEDIUM | COMPENSABLE → remove tag |
| `crm.merge_contacts` | method | dedup/merge | **HIGH** | IRREVERSIBLE (merge loses structure) |
| `crm.list_tags` | query | `crm.tag.search` / `res.partner.category.search` | — | — |

---

## 5. Phase 2 — expansion (C) — deferred until merchants ask

- `crm.create_quotation` / sales order → touches `sale.order`, a **new model surface** = an
  architectural decision (which models we expose), not just a verb. Hold until past the Salla
  milestone; do NOT let it become a second product line.
- `crm.bulk_create_contacts` → first thing to hit the kernel's atomicity limit (see §0.2). Ship
  best-effort bulk only; atomic bulk waits on the kernel mechanism.
- Predictive scoring / company hierarchy / email templates → fields exist, no verbs; lowest priority.

---

## 6. Cross-cutting policies (decide once, apply to every verb)

- **Idempotency:** every write carries an idempotency key (verb + stable args). Upsert dedups on
  normalized phone/email so at-least-once webhooks don't double-write.
- **Field whitelists:** `update_*` only writes declared fields — never blind `write(**args)` (mass-
  assignment / overwriting system fields).
- **Tier policy:** delete/merge ⇒ HIGH; money/ownership-changing ⇒ HIGH; routine writes ⇒ MEDIUM.
  Tier is asserted by conformance, not assumed.
- **Compensation honesty:** if it can't be cleanly reversed, declare IRREVERSIBLE — never fake a
  rollback. Upsert's compensation is conditional on whether it created or updated.

---

## 7. Sequencing (bus-factor 1 — non-negotiable)

1. §1 enabling refactor (op-strategy + system.py I/O + FakeSystem parity) — green conformance.
2. Phase 0, one verb at a time: `get_contact_by_phone` → `update_contact` → upsert → `log_note`.
3. Phase 1, `archive_contact` first, then the rest by the table order.
4. Phase 2 only on merchant demand.

Each verb is DONE only when: adapter code + compensation entry + conformance test (incl. refusal
paths) + manifest declaration are in, and the full matrix is green. No parallel verbs.

---

## 8. Out-of-scope here (separate tasks, flagged)

- Read-path `UNKNOWN_VERB` refusal (contract fix; sync across vendored edges).
- Atomic multi-entity bulk commit (kernel mechanism).
- Any `sale.*` model surface (architectural decision, post-Salla).
