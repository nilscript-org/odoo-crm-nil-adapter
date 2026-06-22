# Root-Cause Report — `verified: true` was unearned ("the app lied")

**Date:** 2026-06-22 · **Severity:** CRITICAL (strikes the core "an agent that can't lie" thesis)
**Reporter trigger:** `crm.update_contact` set `country: السعودية` on contact id 39; COMMIT returned
`claim:success, changed:true, verified:true, read_after_write:true`. The Odoo UI (and a direct SSOT
read) show **Country empty**. Same for `ref`. Confirmed systemic across records (e.g. id 10), not a
one-off.

---

## Verdict

The success envelope was **not earned**. `verified`/`changed`/`read_after_write` were **hardcoded
constants** in the edge — the shim never re-read the record, so it cannot have verified anything.
Separately, fields the verb cannot write (`country`, `ref`) were **echoed back as accepted** at
PROPOSE and then **silently dropped** before the write. Both must be fixed for the envelope to mean
what it says.

---

## Evidence (from the code + the SSOT, not the UI)

### F1 — verification is a literal, not a measurement
`src/odoo_crm_nil_adapter/edge.py` (both COMMIT result sites):

```python
result = {"claim": "success", "changed": True, "verified": True,
          "entity": entity_ref(verb, created),
          "ssot": {"system": SYSTEM, "read_after_write": True}, ...}
```

`changed`, `verified`, and `ssot.read_after_write` are **constant `True`**. There is no read-back, no
field comparison. Every write — even a no-op, even one Odoo silently rejected — reported the same
"verified success." `write_date` did bump (Odoo's `write()` ran), but a timestamp bump is not field
persistence.

### F2 — unsupported args are accepted then dropped
- PROPOSE returns `"resolved": args` — the **raw input**. `country`/`ref` appear there, reading as
  "accepted."
- `_to_native_update_contact` whitelist = `(name, phone, email, comment, company)`. `country`/`ref`
  are **not mapped**, so they never reach `native` and are never written. No error, no warning.
- `country` is additionally a `many2one` (`country_id`): the Arabic label "السعودية" cannot be
  written as a string even if whitelisted — it needs resolution to a `res.country` id.

**Net:** the agent is told `country` was accepted (PROPOSE `resolved`) and then verified (COMMIT
`verified:true`), while it was never written. That is the lie, end to end.

---

## Blast radius

- **Every prior `verified:true` in this product is suspect** for any field outside
  `name/email/phone`. The "30 contacts imported, read-after-write verified" claim is wrong: their
  `country` and `ref` are empty.
- **Benchmark integrity:** any safety number ("0% unauthorized writes over N injection cases") that
  relies on a verifier with the **same blind spot** (checking record existence / timestamp instead
  of field-level intent conformance) is measuring the wrong thing. The harness's verifier must be
  re-audited to confirm it reads back the **semantic effect**, not just that a record was touched.

---

## Root cause — KERNEL, not adapter (confirmed)

The lie originates in the **kernel scaffold template** and is stamped into every adapter it generates.
Three matching sites carry the identical hardcoded literal:

| Source | File | Lines |
|---|---|---|
| **Kernel scaffold template** (the generator) | `nilscript/src/nilscript/cli/scaffold/_templates.py` | 245–247, 283–285 |
| PocketBase adapter (reference / live demo backend) | `nilscript/src/nilscript/demo/pocketbase_nil_adapter/edge.py` | 234–236, 273–275 |
| Odoo adapter | `odoo-crm-nil-adapter/.../edge.py` | 241–243, 310–314 |

The shim was scaffolded to **assert** the NIL guarantees rather than **enforce** them. The success
envelope is the product's entire value (a claim an operator can trust); the **generator** emitted it
as a constant, so every backend inherits the lie and any regeneration re-introduces it. A second
kernel-level fact: the **MCP/kernel passes the adapter envelope through and does not independently
re-verify it** — it trusts a claim the template fabricated. F2 compounds it: a translation layer that
silently discards intent it can't express manufactures false confidence one step earlier, at PROPOSE.

**Fix landed at all three sites** (this change): the generated edge now re-reads the SSOT and earns
`verified`; `_verify_write`/`_field_landed` replace the constant. Scaffold tests + both adapters'
conformance suites are green.

---

## Permanent fix (this PR)

1. **Earn `verified` (F1).** After a write, re-read the record from the SSOT and compare every field
   the verb actually wrote (`native`). `verified = no field mismatched`. A mismatch ⇒
   `verified:false`, `claim:"partial"`, and a structured `unverified_fields:[...]` naming what didn't
   land. Comparison tolerates documented backend normalization (html wrapping, formatting) but treats
   **empty/false-where-a-value-was-intended as a hard miss** — the exact `country_id:false` signature.
2. **Stop silent drops (F2).** A verb declares the NIL args it can write (`supported_args`). PROPOSE
   surfaces any provided arg outside that set in an `ignored:[...]` list, so `country`/`ref` are
   visibly *not accepted* instead of echoed as resolved.

## Out of this PR (follow-ups, named so they're not silently skipped)

- **Country as a writable field** needs `res.country` resolution (Arabic + English label → id) and a
  whitelist entry. Until then `country` is honestly reported as `ignored`, not faked.
- **Kernel/benchmark verifier re-audit** — confirm it compares field-level effect, not existence.
  This report's F1 is the adapter-side fix; the harness needs the same honesty check.
- **`ref`/other res.partner fields** — add to the whitelist as needed once each has a defined mapping.
