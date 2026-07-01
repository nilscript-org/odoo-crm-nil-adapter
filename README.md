# odoo-nil-adapter

**A NIL governed-action adapter for Odoo CRM.** It speaks the NIL contract (propose -> approve ->
commit, skeleton-bounded, earned reversibility): an agent proposes intent, the deterministic kernel
is the only thing that commits, and a verb or target Odoo never declared is unexpressible, not
filtered.

It is a **conformant NIL translation shim for [Odoo CRM](https://www.odoo.com/app/crm)** — it speaks
the six NIL endpoints (+ `/nil/v0.1/describe`) at the edge and translates each verb into native Odoo
records over the [XML-RPC External API](https://www.odoo.com/documentation/master/developer/reference/external_api.html).
A NIL `target` is simply an Odoo model name (`crm.lead`, `res.partner`, `crm.stage`).

> Built from [`nil-adapter-template`](https://github.com/nilscript-org/nil-adapter-template). The
> edge / state / models / manifest loader are the generic, unmodified kernel output; only
> `system.py`, `translate.py`, and `compensation.py` are Odoo-specific.

## What it exposes

**Generic `resource.*` CRUD** — `create / read / update / delete` against **any** Odoo model, with
synthesized reversibility (create→delete, update→restore-before-image, delete→recreate). No
per-model authoring.

**Semantic `crm.*` verbs** — curated args, bilingual (ar/en) previews, and tiers:

| Verb | Odoo model | Tier | Reversible? |
|------|-----------|------|-------------|
| `crm.create_lead` | `crm.lead` | MEDIUM | ✅ REVERSIBLE (→ `crm.delete_lead`) |
| `crm.create_contact` | `res.partner` | MEDIUM | ✅ REVERSIBLE (→ `crm.delete_contact`) |
| `crm.update_lead_stage` | `crm.lead` | MEDIUM | IRREVERSIBLE¹ |
| `crm.delete_lead` | `crm.lead` | HIGH | IRREVERSIBLE |
| `crm.delete_contact` | `res.partner` | HIGH | IRREVERSIBLE |

Read-through (no side effects): `crm.list_leads`, `crm.list_contacts`, `crm.list_stages`.

¹ For a reversible field update, use the generic `resource.update` family — the edge captures a
before-image and synthesizes a COMPENSABLE restore.

## Configure (credentials live in the environment only)

```bash
cp .env.example .env        # .env is git-ignored — never commit it
# edit .env:
#   ODOO_URL=https://wosool.odoo.com
#   ODOO_DB=wosool
#   ODOO_LOGIN=you@example.com   # the user the API key belongs to
#   ODOO_API_KEY=...             # Settings → Users → API Keys
```

Create the API key in Odoo under **Settings → Users → Account Security → API Keys**. The key is used
as the password in `authenticate(db, login, key)`; **the adapter never stores it** beyond the process
environment.

## Run

```bash
export $(grep -v '^#' .env | xargs)    # load .env
uvicorn odoo_nil_adapter.run_live:build_app --factory --host 0.0.0.0 --port 8099
# discovery handshake:
curl -s localhost:8099/nil/v0.1/describe | python -m json.tool
```

## Verify (offline, no live instance)

```bash
PYTHONPATH=src pytest conformance/ -q     # conformance proof against the in-memory FakeSystem
```

The proof drives PROPOSE→COMMIT per write verb, checks ROLLBACK honesty (a reversal previews, never
silently writes; an unknown token is refused), and that `describe` exposes a valid skeleton.

## Citation

This adapter is part of the **NIL (Network Intent Layer)** framework, described in a published paper
archived on Zenodo with a permanent DOI:
**[10.5281/zenodo.20774491](https://doi.org/10.5281/zenodo.20774491)**.

```bibtex
@misc{elkhider2026nil,
  title  = {Unexpressible, Not Filtered: A Structural Framework for Governing AI-Agent Actions --- the Network Intent Layer},
  author = {Elkhider, ElBasheir A. M.},
  year   = {2026},
  doi    = {10.5281/zenodo.20774491},
  url    = {https://doi.org/10.5281/zenodo.20774491}
}
```
