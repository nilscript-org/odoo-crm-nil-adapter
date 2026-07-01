# Design: `odoo-nil-adapter` — general Odoo adapter with per-module verb packs

**Date:** 2026-07-01
**Status:** Approved (brainstorming) → pending implementation plan
**Repo:** `adapters/odoo-crm-nil-adapter` → renamed `adapters/odoo-nil-adapter`

## Problem

The adapter is named `odoo-crm-nil-adapter`, but its core is already model-agnostic:

- **Reads** are universal — `nil.*` discovers *any* Odoo model live via `fields_get`
  (`read_plane.py`). Nothing CRM-specific.
- **Writes** already reach any model via `resource.*` + `resource.method`, gated by
  governance grants + module groups. Not CRM-locked.

The only remaining "CRM-ness" is three vestiges:

1. **The name** — repo `odoo-crm-nil-adapter`, package `odoo_crm_nil_adapter`.
2. **The default write skeleton** — `DECLARED_TARGETS` ships CRM models, so out-of-the-box
   writes default to CRM; other modules need a grant.
3. **Curated sugar** — the `crm.*` semantic verbs and hand-tuned `_TARGET_FIELDS` projections
   (alongside already-present `account.*` / `sale.*` / `stock.*` ones).

This is a reframe + rename, not a rewrite. It also closes a concrete gap: **procurement**
(`purchase.order` create + confirm, catalogue, stock) is currently unexpressible for writes —
there is not even a `purchasing` module group in governance.

## Decisions (from brainstorming)

| Decision | Choice |
|---|---|
| Scope | Rename **and** restructure curated verbs into per-module packs |
| Default enablement | **All packs on** by default; `NIL_ENABLED_MODULES` narrows at runtime |
| Purchasing | **Build the purchasing pack now** (first new pack) |
| Name | **`odoo-nil-adapter`** (package `odoo_nil_adapter`) |

## Architecture

### 1. Pack registry (thin — data, not a framework)

New `packs.py`:

```python
@dataclass(frozen=True)
class ModulePack:
    name: str                        # "crm" | "finance" | "sales" | "inventory" | "purchasing"
    model_prefixes: tuple[str, ...]  # → governance module groups
    write_targets: tuple[str, ...]   # → default committable write skeleton (at CRUD tiers)
    method_grants: tuple[tuple[str, str, str, str | None], ...]  # (model, method, tier, reverse)
    write_verbs: tuple[WriteVerb, ...]       # semantic sugar
    query_verbs: tuple[QueryVerb, ...]
    projections: dict[str, tuple[str, ...]]  # curated read fields
    sensitive: dict[str, frozenset[str]]

PACKS: tuple[ModulePack, ...] = (CRM, FINANCE, SALES, INVENTORY, PURCHASING)
```

`translate.py`, `governance.py`, and `read_plane.py` stop hardcoding their tables and
**aggregate them from enabled packs** instead:

- `translate.py`: `WRITE_VERBS` / `QUERY_VERBS` / `DECLARED_TARGETS` = union over enabled packs.
- `governance.py`: `_MODULE_MODELS`, `_DEFAULT_WRITE`, `_DEFAULT_METHOD` derive from packs.
- `read_plane.py`: `_TARGET_FIELDS`, `_SENSITIVE` aggregate from packs.

One place per module answers "what does this module contribute."

### 2. The five packs

- **crm / finance / sales / inventory** — migrated *verbatim* from today's `WRITE_VERBS` /
  `QUERY_VERBS` / `_TARGET_FIELDS`. Behavior identical; the existing test suite is the
  migration's correctness proof.
- **purchasing (new):**
  - `write_targets`: `purchase.order` (create, HIGH)
  - `purchase.create_order` — `partner_id` + `lines`, shaped to
    `order_line=[(0, 0, {product_id, product_qty, product_uom, price_unit, taxes_id:[(6,0,[…])]})]`
    (x2many command tuples already pass through the edge verbatim).
  - `purchase.confirm_order` — `op="method"`, `method="button_confirm"`,
    **`reverse_method="button_cancel"` → COMPENSABLE** (ROLLBACK cancels the PO).
  - `model_prefixes`: `purchase.`, `uom.` — fills the missing module group.
  - projections for `purchase.order`, `purchase.order.line`, `product.product`, `stock.quant`.

### 3. Default posture: all packs on

`_ENABLED_MODULES = None` = all packs active. `NIL_ENABLED_MODULES` env narrows at runtime
with no code change. Enabled ⇒ discoverable + curated verbs available + the pack's
`write_targets` in the default committable skeleton at their declared tiers.

### 4. Write-ceiling invariant (the guardrail)

"Pack enabled" widens *discovery + curated verbs*, **not** raw write authz beyond each pack's
declared targets/tiers:

- Irreversible verbs stay HIGH / owner-reviewed behind the two-step gate.
- `purchase.confirm_order` is compensable (reverse method), not IRREVERSIBLE.
- `test_unexpressibility.py` stays green — an undeclared write is still `β⁻¹(a) = ∅`.
- Per-tenant `NIL_TENANT_GRANTS` remains the seam to widen *past* the shipped skeleton
  (e.g. `hr.employee`).

## Data flow (unchanged shapes)

`nil.*` reads → `ReadPlane` → `OdooReadBackend` → `search_read`/`search_count`.
`resource.*` / semantic writes → PROPOSE (tier + bilingual preview, no side effect) →
COMMIT (dispatch on `op`) → verify landed → optional COMPENSATE on ROLLBACK.

## Rename surface (the real mechanical risk)

- `adapters/odoo-crm-nil-adapter/` → `adapters/odoo-nil-adapter/`.
- Package `odoo_crm_nil_adapter` → `odoo_nil_adapter` (every import), `pyproject.toml`,
  `egg-info`.
- Callers in the `nilscript` repo that import the package — grep-and-update.
- Deploy: playground Dockerfile install path `/app/odoo-crm-nil-adapter` → `/app/odoo-nil-adapter`;
  `NIL_SELF_URL_ODOO` unchanged (URL, not path).
- Hermes skill reference `~/.hermes/skills/integration/odoo-crm-nilscript` — note/update.
- Ships **with** the required image rebuild + redeploy (production runs a stale pre-governance
  image today; this drift is corrected in the same deploy).

## Consequence: staged grant becomes redundant

A `NIL_TENANT_GRANTS` entry (workspace `ws_acme`, `purchase.order` create + `button_confirm`)
was staged on the live host's `nilscript-landing` `.env` + compose. With purchasing in the
default skeleton, that grant is **redundant** — remove it as part of this change so there is one
source of truth. (Backups: `.bak.grants` on both files.)

## Testing (TDD)

Write purchasing-pack tests **first**, then do the registry refactor under the existing green
suite:

- `purchase.create_order` builds correct `order_line` / `taxes_id` command tuples.
- `purchase.confirm_order` proposes at HIGH and ROLLBACK runs `button_cancel` (compensable).
- Unexpressibility: an ungranted `purchase.*` model / method is refused (β⁻¹ = ∅).
- Module scope: `NIL_ENABLED_MODULES` excluding `purchasing` makes purchase models
  undiscoverable **and** unwritable.
- Regression: the full existing suite stays green after the pack migration (proves crm /
  finance / sales / inventory behavior is unchanged).

## Non-goals (YAGNI)

- No plugin/extension framework — packs are a frozen dataclass + a tuple.
- No new modules beyond the five (mrp / hr / project stay generic-plane-only until a real flow
  needs sugar).
- No change to the read projection algorithm, byte-cap, export, or intent resolver.
- No multi-tenant SaaS posture change — single-instance default stands per MVP scope.
