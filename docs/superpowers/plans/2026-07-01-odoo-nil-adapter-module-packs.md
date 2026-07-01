# odoo-nil-adapter Module Packs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `odoo-crm-nil-adapter` into a general `odoo-nil-adapter` whose curated verbs, write skeleton, module groups, and read projections are contributed by per-module packs — and add a new `purchasing` pack that closes the procurement (PO) write gap.

**Architecture:** Introduce a frozen `ModulePack` dataclass and a `PACKS` registry (`packs.py`). `translate.py`, `governance.py`, and `read_plane.py` stop hardcoding their tables and aggregate them from enabled packs. Existing crm/finance/sales/inventory verbs move into packs verbatim (existing conformance suite proves behavior unchanged); a new purchasing pack is added TDD-first. Then rename the package/repo/deploy path and rebuild the playground image.

**Tech Stack:** Python 3.12, frozen `@dataclass`, pytest (`conformance/`), FastAPI edge, Odoo XML-RPC via `SystemClient`.

## Global Constraints

- Python 3.12+; no new third-party dependencies.
- All new domain data structures are `@dataclass(frozen=True)` (immutability).
- The **write-ceiling invariant** holds: "pack enabled" ⇒ discoverable + curated verbs available, NOT raw write authz beyond each pack's declared targets/tiers. `conformance/test_unexpressibility.py` MUST stay green.
- The full existing `conformance/` suite MUST stay green after every migration task (behavior-preserving refactor).
- Irreversible workflow verbs keep tier `HIGH`; `purchase.confirm_order` is COMPENSABLE via `reverse_method="button_cancel"`, never IRREVERSIBLE.
- Package rename is atomic: `odoo_crm_nil_adapter` → `odoo_nil_adapter` everywhere, no shim left behind.
- Run tests from the adapter repo root with `.venv`: `cd adapters/odoo-crm-nil-adapter && python -m pytest`.

---

### Task 1: `ModulePack` dataclass + registry scaffolding, CRM pack, translate aggregation

**Files:**
- Create: `src/odoo_crm_nil_adapter/packs.py`
- Modify: `src/odoo_crm_nil_adapter/translate.py` (replace literal `WRITE_VERBS` / `QUERY_VERBS` / `DECLARED_TARGETS` construction with aggregation over packs)
- Test: `conformance/test_packs.py`

**Interfaces:**
- Produces:
  - `ModulePack` frozen dataclass with fields: `name: str`, `model_prefixes: tuple[str, ...]`, `write_targets: tuple[str, ...]`, `method_grants: tuple[tuple[str, str, str, str | None], ...]`, `write_verbs: tuple[WriteVerb, ...]`, `query_verbs: tuple[QueryVerb, ...]`, `projections: dict[str, tuple[str, ...]]`, `sensitive: dict[str, frozenset[str]]`.
  - `PACKS: tuple[ModulePack, ...]`.
  - `enabled_packs() -> tuple[ModulePack, ...]` — filters `PACKS` by `governance.module_enabled(pack.name)` (a pack is a module group).
  - Aggregators consumed by later tasks: `all_write_verbs() -> dict[str, WriteVerb]`, `all_query_verbs() -> dict[str, QueryVerb]`, `all_write_targets() -> frozenset[str]`.
- Consumes: existing `WriteVerb` / `QueryVerb` dataclasses from `translate.py`. To avoid an import cycle (`translate` ↔ `packs`), `packs.py` imports the verb *definitions* that currently live in `translate.py`; move the `WriteVerb`/`QueryVerb` dataclasses and the `_to_native_*` / `_run_*` helpers into `packs.py`-adjacent modules only if a cycle appears — first attempt keeps `WriteVerb`/`QueryVerb` in `translate.py` and has `packs.py` import them.

- [ ] **Step 1: Write the failing test**

```python
# conformance/test_packs.py
from odoo_crm_nil_adapter import packs, translate


def test_registry_exposes_crm_pack():
    names = {p.name for p in packs.PACKS}
    assert "crm" in names


def test_translate_write_verbs_come_from_packs():
    # crm.create_lead is contributed by the crm pack and surfaces in the aggregate
    assert "crm.create_lead" in translate.WRITE_VERBS
    assert translate.WRITE_VERBS["crm.create_lead"].doctype == "crm.lead"


def test_declared_targets_is_union_of_pack_write_targets():
    assert "crm.lead" in translate.DECLARED_TARGETS
    assert "res.partner" in translate.DECLARED_TARGETS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest conformance/test_packs.py -v`
Expected: FAIL — `ModuleNotFoundError: odoo_crm_nil_adapter.packs`.

- [ ] **Step 3: Create `packs.py` with the dataclass, the CRM pack, and aggregators**

```python
# src/odoo_crm_nil_adapter/packs.py
"""Per-module verb packs: the single place each Odoo module declares what it contributes —
curated write/query verbs, its default write skeleton, method grants, module-group prefixes,
and read projections. translate.py / governance.py / read_plane.py aggregate over ENABLED packs
instead of hardcoding tables. A pack's `name` IS its governance module group."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from odoo_crm_nil_adapter.translate import QueryVerb, WriteVerb


@dataclass(frozen=True)
class ModulePack:
    name: str
    model_prefixes: tuple[str, ...]
    write_targets: tuple[str, ...] = ()
    method_grants: tuple[tuple[str, str, str, str | None], ...] = ()
    write_verbs: tuple[WriteVerb, ...] = ()
    query_verbs: tuple[QueryVerb, ...] = ()
    projections: dict[str, tuple[str, ...]] = field(default_factory=dict)
    sensitive: dict[str, frozenset[str]] = field(default_factory=dict)


# CRM pack — the write_verbs / query_verbs tuples reference the verb objects that already exist
# in translate.py (moved to module-level constants there in Step 4). Reference models kept so the
# curated resolvers (country lookup) still resolve.
CRM = ModulePack(
    name="crm",
    model_prefixes=("crm.", "res.partner", "res.country", "res.country.state", "res.partner.category"),
    write_targets=(
        "crm.lead", "res.partner", "crm.stage", "crm.tag",
        "res.partner.category", "crm.team", "res.country", "res.country.state",
    ),
    method_grants=(
        ("res.partner", "message_post", "MEDIUM", None),
        ("crm.lead", "message_post", "MEDIUM", None),
    ),
    write_verbs=(),   # populated in Step 4 once verb constants are exported
    query_verbs=(),
    projections={
        "res.partner": ("id", "name", "phone", "email"),
        "crm.lead": ("id", "name", "contact_name", "email_from", "phone", "stage_id", "expected_revenue"),
        "crm.stage": ("id", "name", "sequence"),
        "crm.team": ("id", "name"),
        "res.country": ("id", "name", "code"),
    },
    sensitive={"res.partner": frozenset({"credit_limit", "vat"})},
)

PACKS: tuple[ModulePack, ...] = (CRM,)


def enabled_packs() -> tuple[ModulePack, ...]:
    from odoo_crm_nil_adapter import governance
    return tuple(p for p in PACKS if governance.module_enabled(p.name))


def all_write_verbs() -> dict[str, WriteVerb]:
    out: dict[str, WriteVerb] = {}
    for p in enabled_packs():
        for v in p.write_verbs:
            out[v.verb] = v
    return out


def all_query_verbs() -> dict[str, QueryVerb]:
    out: dict[str, QueryVerb] = {}
    for p in enabled_packs():
        for v in p.query_verbs:
            out[v.verb] = v
    return out


def all_write_targets() -> frozenset[str]:
    targets: set[str] = set()
    for p in enabled_packs():
        targets.update(p.write_targets)
    return frozenset(targets)
```

- [ ] **Step 4: Wire `translate.py` to the CRM pack**

In `translate.py`: keep every existing `WriteVerb(...)` / `QueryVerb(...)` object but assign each to a module-level constant (e.g. `CRM_CREATE_LEAD = WriteVerb(...)`), then set the CRM pack's `write_verbs` / `query_verbs` in `packs.py` to reference those constants. Replace the literal `DECLARED_TARGETS = frozenset({...})` and the `WRITE_VERBS = {...}` / `QUERY_VERBS = {...}` dict literals with:

```python
# translate.py — after the nil.* query verbs are registered
from odoo_crm_nil_adapter import packs  # noqa: E402  (late import: packs imports WriteVerb/QueryVerb)

DECLARED_TARGETS: frozenset[str] = packs.all_write_targets()
WRITE_VERBS: dict[str, WriteVerb] = packs.all_write_verbs()
QUERY_VERBS: dict[str, QueryVerb] = {**packs.all_query_verbs(), **_NIL_QUERY_VERBS}
```

Keep the `nil.*` universal read verbs (`nil.search/count/get/aggregate/export/intent`) in a `_NIL_QUERY_VERBS` dict in `translate.py` (they are cross-module infrastructure, not a pack). The CRM pack's `query_verbs` gets `crm.list_leads/list_contacts/list_stages/list_countries/get_contact_by_phone`.

> Note: `DECLARED_TARGETS`/`WRITE_VERBS`/`QUERY_VERBS` become module-level values evaluated at import. Because `governance.module_enabled` defaults to all-enabled, the aggregate equals today's literal set. Downstream imports (`from ... import DECLARED_TARGETS`) keep working.

- [ ] **Step 5: Run the new test + full suite**

Run: `python -m pytest conformance/test_packs.py conformance/test_conformance.py conformance/test_unexpressibility.py -v`
Expected: PASS (new tests green; existing behavior unchanged).

- [ ] **Step 6: Commit**

```bash
git add src/odoo_crm_nil_adapter/packs.py src/odoo_crm_nil_adapter/translate.py conformance/test_packs.py
git commit -m "refactor(packs): ModulePack registry + CRM pack; translate aggregates from packs"
```

---

### Task 2: Migrate finance / sales / inventory packs + governance & read_plane aggregation

**Files:**
- Modify: `src/odoo_crm_nil_adapter/packs.py` (add FINANCE, SALES, INVENTORY packs; extend `PACKS`)
- Modify: `src/odoo_crm_nil_adapter/governance.py` (derive `_MODULE_MODELS`, `_DEFAULT_WRITE`, `_DEFAULT_METHOD` from packs)
- Modify: `src/odoo_crm_nil_adapter/read_plane.py` (derive `_TARGET_FIELDS`, `_SENSITIVE` from packs)
- Test: `conformance/test_packs.py` (extend)

**Interfaces:**
- Consumes: `packs.enabled_packs()`, `packs.all_write_targets()` from Task 1.
- Produces:
  - `packs.module_models() -> dict[str, tuple[str, ...]]` — `{pack.name: pack.model_prefixes}`.
  - `packs.default_methods() -> dict[tuple[str, str], MethodGrant]` — from every enabled pack's `method_grants`.
  - `packs.all_projections() -> dict[str, tuple[str, ...]]` and `packs.all_sensitive() -> dict[str, frozenset[str]]`.

- [ ] **Step 1: Write the failing test**

```python
def test_finance_and_inventory_packs_present():
    names = {p.name for p in packs.PACKS}
    assert {"finance", "sales", "inventory"} <= names


def test_governance_module_models_from_packs():
    from odoo_crm_nil_adapter import governance
    assert governance.model_class("account.move") == "financial"
    # inventory prefixes come from the pack
    assert governance.module_enabled("stock.picking") is True


def test_read_projection_from_pack():
    from odoo_crm_nil_adapter import read_plane
    assert read_plane._TARGET_FIELDS["account.move"][0] == "id"
    assert "amount_total" in read_plane._TARGET_FIELDS["account.move"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest conformance/test_packs.py -v`
Expected: FAIL — finance/sales/inventory not in `PACKS`.

- [ ] **Step 3: Add the three packs (verbatim migration)**

In `packs.py`, add `FINANCE`, `SALES`, `INVENTORY` packs. Move the existing verb objects into module-level constants (as in Task 1 Step 4) and reference them:

- `FINANCE`: `model_prefixes=("account.",)`; `write_targets=()` (finance writes stay grant-only today — do NOT add account.* to the default skeleton); `write_verbs=(ACCOUNT_CREATE_INVOICE, ACCOUNT_POST_INVOICE, ACCOUNT_REGISTER_PAYMENT)`; `method_grants=()`; projections for `account.move/account.payment/account.move.line/account.journal/account.account/account.tax` (move the existing `_TARGET_FIELDS` finance entries here verbatim); `sensitive={}`.
- `SALES`: `model_prefixes=("sale.",)`; `write_verbs=(SALE_CONFIRM_ORDER,)`.
- `INVENTORY`: `model_prefixes=("stock.", "product.")`; `write_verbs=(STOCK_VALIDATE_PICKING,)`.

Add `module_models()`, `default_methods()`, `all_projections()`, `all_sensitive()` aggregators. Extend `PACKS = (CRM, FINANCE, SALES, INVENTORY)`.

> The existing `account.*`/`sale.*`/`stock.*` semantic verbs are workflow methods gated by `_DEFAULT_METHOD` grants keyed off the tenant/onboarding path — keep those method grants exactly where they are (empty default) so behavior is unchanged. `write_targets` stays CRM-only in the default skeleton (finance/inventory raw writes remain grant-only), preserving the write ceiling.

- [ ] **Step 4: Derive governance tables from packs**

In `governance.py`, replace the literal `_MODULE_MODELS` and the `_DEFAULT_METHOD` seed and `_DEFAULT_WRITE` with pack-derived values. Import lazily to avoid the cycle:

```python
def _packs():
    from odoo_crm_nil_adapter import packs
    return packs

# _MODULE_MODELS: built from packs at first use (module_enabled already tolerates unknown groups)
def _module_models() -> dict[str, tuple[str, ...]]:
    return _packs().module_models()

# _DEFAULT_WRITE stays: {(m, op): tier for m in DECLARED_TARGETS for op, tier in _CRUD_TIERS.items()}
# but DECLARED_TARGETS now == packs.all_write_targets()
```

Update `module_enabled()` to consult `_module_models()` and `_DEFAULT_METHOD` lookups to consult `_packs().default_methods()`. Keep the per-tenant overlay logic untouched.

- [ ] **Step 5: Derive read_plane tables from packs**

In `read_plane.py`, replace the literal `_TARGET_FIELDS` and `_SENSITIVE` dicts with:

```python
def _target_fields() -> dict[str, tuple[str, ...]]:
    from odoo_crm_nil_adapter import packs
    return packs.all_projections()

# describe_target(): fields = _target_fields().get(target) or derive from fields_get
```

Keep `_SENSITIVE_FRAGMENTS` (cross-module PII heuristics) in `read_plane.py` — it is not per-pack.

- [ ] **Step 6: Run full suite**

Run: `python -m pytest conformance/ -v`
Expected: PASS — entire existing suite green (behavior identical), plus new pack tests.

- [ ] **Step 7: Commit**

```bash
git add src/odoo_crm_nil_adapter/packs.py src/odoo_crm_nil_adapter/governance.py src/odoo_crm_nil_adapter/read_plane.py conformance/test_packs.py
git commit -m "refactor(packs): finance/sales/inventory packs; governance+read_plane aggregate from packs"
```

---

### Task 3: Purchasing pack (TDD — new verbs)

**Files:**
- Modify: `src/odoo_crm_nil_adapter/translate.py` (add `_to_native_create_po` shaper + verb constants) OR add them in a new `src/odoo_crm_nil_adapter/purchasing.py` imported by `packs.py`
- Modify: `src/odoo_crm_nil_adapter/packs.py` (add PURCHASING pack; extend `PACKS`)
- Modify: `src/odoo_crm_nil_adapter/compensation.py` (register `button_confirm` → `button_cancel` reversibility if it drives compensation there)
- Test: `conformance/test_purchasing.py`

**Interfaces:**
- Consumes: `WriteVerb` (with `op`, `method`, `reverse_method`, `references`, `supported_args`), `packs.ModulePack`.
- Produces:
  - `PURCHASE_CREATE_ORDER: WriteVerb` — `verb="purchase.create_order"`, `op="create"`, `doctype="purchase.order"`, `tier="HIGH"`, `required=("partner_id",)`, `supported_args=("partner_id","date_order","date_planned","origin","priority","currency_id","company_id","user_id","lines")`.
  - `PURCHASE_CONFIRM_ORDER: WriteVerb` — `verb="purchase.confirm_order"`, `op="method"`, `method="button_confirm"`, `reverse_method="button_cancel"`, `tier="HIGH"`, `required=("order_id",)`.
  - `PURCHASING: ModulePack` with `model_prefixes=("purchase.","uom.")`, `write_targets=("purchase.order",)`, `method_grants=(("purchase.order","button_confirm","HIGH","button_cancel"),)`, projections for `purchase.order`/`purchase.order.line`/`product.product`/`stock.quant`.

- [ ] **Step 1: Write the failing tests**

```python
# conformance/test_purchasing.py
from odoo_crm_nil_adapter import packs, translate, governance


def test_purchase_create_order_shapes_x2many_order_line():
    v = translate.WRITE_VERBS["purchase.create_order"]
    doc = v.to_native({
        "partner_id": 42, "date_order": "2026-06-25 00:00:00", "origin": "SEWAR-REPLEN-0001",
        "lines": [{"product_id": 1001, "product_qty": 100, "product_uom": 1,
                   "price_unit": 3.5, "taxes_id": [5]}],
    })
    assert doc["partner_id"] == 42
    assert doc["origin"] == "SEWAR-REPLEN-0001"
    # order_line is Odoo create-child command tuples
    assert doc["order_line"][0][0] == 0 and doc["order_line"][0][1] == 0
    line = doc["order_line"][0][2]
    assert line["product_id"] == 1001 and line["product_qty"] == 100.0
    assert line["price_unit"] == 3.5
    # taxes_id is the (6,0,[ids]) replace command
    assert line["taxes_id"] == [(6, 0, [5])]


def test_purchase_confirm_is_compensable():
    v = translate.WRITE_VERBS["purchase.confirm_order"]
    assert v.op == "method" and v.method == "button_confirm"
    assert v.reverse_method == "button_cancel"


def test_purchase_order_writable_and_confirm_granted_by_default():
    governance.reset_policy()  # all modules enabled
    assert governance.write_tier("purchase.order", "create") == "HIGH"
    assert governance.method_tier("purchase.order", "button_confirm") == "HIGH"
    assert governance.method_reverse("purchase.order", "button_confirm") == "button_cancel"


def test_purchasing_unexpressible_when_module_disabled():
    governance.set_enabled_modules({"crm"})  # purchasing OUT of scope
    assert governance.write_tier("purchase.order", "create") is None
    assert governance.method_tier("purchase.order", "button_confirm") is None
    governance.reset_policy()


def test_purchasing_ungranted_model_still_denied():
    # a purchase.* model NOT declared by the pack is unexpressible even with purchasing enabled
    governance.reset_policy()
    assert governance.write_tier("purchase.requisition", "create") is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest conformance/test_purchasing.py -v`
Expected: FAIL — `purchase.create_order` not in `WRITE_VERBS`.

- [ ] **Step 3: Implement the PO shaper + verbs**

Add to `translate.py`:

```python
def _po_line(ln: dict[str, Any]) -> dict[str, Any]:
    """One purchase.order.line as Odoo write form. taxes_id becomes the (6,0,[ids]) replace command."""
    line: dict[str, Any] = {
        "product_id": _maybe_int(ln.get("product_id")),
        "product_qty": _maybe_float(ln.get("product_qty", 1)),
        "price_unit": _maybe_float(ln.get("price_unit", 0)),
    }
    if ln.get("product_uom") is not None:
        line["product_uom"] = _maybe_int(ln["product_uom"])
    if ln.get("date_planned"):
        line["date_planned"] = ln["date_planned"]
    taxes = ln.get("taxes_id")
    if taxes:
        line["taxes_id"] = [(6, 0, [_maybe_int(t) for t in taxes])]
    return line


def _to_native_create_po(args: dict[str, Any]) -> dict[str, Any]:
    """NIL args → an Odoo purchase.order. Lines use the [(0,0,{…})] create-child form."""
    doc: dict[str, Any] = {"partner_id": _maybe_int(args["partner_id"])}
    for k in ("date_order", "date_planned", "origin", "priority"):
        if args.get(k):
            doc[k] = args[k]
    for k in ("currency_id", "company_id", "user_id"):
        if args.get(k):
            doc[k] = _maybe_int(args[k])
    lines = args.get("lines") or []
    if lines:
        doc["order_line"] = [(0, 0, _po_line(ln)) for ln in lines if isinstance(ln, dict)]
    return doc


PURCHASE_CREATE_ORDER = WriteVerb(
    verb="purchase.create_order", tier="HIGH", doctype="purchase.order", op="create",
    required=("partner_id",), to_native=_to_native_create_po,
    preview=lambda a: {
        "en": f"Create purchase order for vendor {a.get('partner_id', '')}"
        + (f" ({len(a['lines'])} line(s))" if a.get("lines") else ""),
        "ar": f"إنشاء أمر شراء للمورد {a.get('partner_id', '')}"
        + (f" ({len(a['lines'])} بند)" if a.get("lines") else ""),
    },
    entity_type="purchase_order",
    supported_args=("partner_id", "date_order", "date_planned", "origin", "priority",
                    "currency_id", "company_id", "user_id", "lines"),
)

PURCHASE_CONFIRM_ORDER = WriteVerb(
    verb="purchase.confirm_order", tier="HIGH", doctype="purchase.order", op="method",
    method="button_confirm", reverse_method="button_cancel", required=("order_id",),
    to_native=_to_native_method_only,
    preview=lambda a: {
        "en": f"Confirm purchase order {a.get('order_id', '')}",
        "ar": f"تأكيد أمر الشراء {a.get('order_id', '')}",
    },
    entity_type="purchase_order",
)
```

- [ ] **Step 4: Register the PURCHASING pack**

In `packs.py`:

```python
from odoo_crm_nil_adapter.translate import PURCHASE_CREATE_ORDER, PURCHASE_CONFIRM_ORDER

PURCHASING = ModulePack(
    name="purchasing",
    model_prefixes=("purchase.", "uom."),
    write_targets=("purchase.order",),
    method_grants=(("purchase.order", "button_confirm", "HIGH", "button_cancel"),),
    write_verbs=(PURCHASE_CREATE_ORDER, PURCHASE_CONFIRM_ORDER),
    query_verbs=(),
    projections={
        "purchase.order": ("id", "name", "partner_id", "date_order", "date_planned",
                           "state", "origin", "currency_id", "amount_total"),
        "purchase.order.line": ("id", "name", "order_id", "product_id", "product_qty",
                                "product_uom", "price_unit", "date_planned"),
        "product.product": ("id", "default_code", "name", "barcode", "uom_id",
                            "uom_po_id", "list_price", "standard_price"),
        "stock.quant": ("id", "product_id", "location_id", "quantity"),
    },
    sensitive={},
)

PACKS = (CRM, FINANCE, SALES, INVENTORY, PURCHASING)
```

- [ ] **Step 5: Run purchasing tests + full suite**

Run: `python -m pytest conformance/test_purchasing.py conformance/test_unexpressibility.py conformance/ -v`
Expected: PASS — purchasing verbs work, unexpressibility invariant intact, whole suite green.

- [ ] **Step 6: Commit**

```bash
git add src/odoo_crm_nil_adapter/translate.py src/odoo_crm_nil_adapter/packs.py conformance/test_purchasing.py
git commit -m "feat(purchasing): purchase.create_order + compensable purchase.confirm_order pack"
```

---

### Task 4: Rename package `odoo_crm_nil_adapter` → `odoo_nil_adapter`

**Files:**
- Rename: `src/odoo_crm_nil_adapter/` → `src/odoo_nil_adapter/` (git mv)
- Modify: `pyproject.toml` (`name`, `description`, packages)
- Modify: every `from odoo_crm_nil_adapter` / `import odoo_crm_nil_adapter` across `src/` and `conformance/`
- Modify: `README.md`, `requirements-manifest.json` if they reference the module path

**Interfaces:**
- Produces: importable package `odoo_nil_adapter` with identical public surface (`edge.create_app`, `system.RealSystemClient`, `run_live.build_app`, `run.build_demo_app`).

- [ ] **Step 1: Rename the package directory and update pyproject**

```bash
git mv src/odoo_crm_nil_adapter src/odoo_nil_adapter
sed -i 's/odoo-crm-nil-adapter/odoo-nil-adapter/g; s/odoo_crm_nil_adapter/odoo_nil_adapter/g' pyproject.toml
```

- [ ] **Step 2: Rewrite imports across source and tests**

```bash
grep -rl 'odoo_crm_nil_adapter' src conformance README.md requirements-manifest.json 2>/dev/null \
  | xargs sed -i 's/odoo_crm_nil_adapter/odoo_nil_adapter/g'
```

- [ ] **Step 3: Reinstall editable + run the full suite under the new name**

Run:
```bash
pip install -e . -q && python -m pytest conformance/ -v
```
Expected: PASS — entire suite green under `odoo_nil_adapter`. If any `ModuleNotFoundError: odoo_crm_nil_adapter` remains, grep for the stray reference and fix.

- [ ] **Step 4: Verify no stale references remain**

Run: `grep -rn 'odoo_crm_nil_adapter\|odoo-crm-nil-adapter' src conformance pyproject.toml README.md`
Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: rename package odoo_crm_nil_adapter -> odoo_nil_adapter"
```

---

### Task 5: Rename repo dir + deploy wiring + remove staged grant

**Files:**
- Rename (in `nilscript-landing` repo): none in adapter repo; the deploy repo references change.
- Modify: `nilscript-landing/.github/workflows/deploy.yml:57-58` (`repository:` + `path:`)
- Modify: `nilscript-landing/deploy/playground.Dockerfile:15,17` (COPY + pip install path)
- Modify (host, live): remove `NIL_TENANT_GRANTS` line from `/root/nilscript-landing/.env` and its passthrough from `docker-compose.prod.yml` (backups `.bak.grants` exist)
- External: rename GitHub repo `nilscript-org/odoo-crm-nil-adapter` → `nilscript-org/odoo-nil-adapter` (GitHub keeps redirects; still update `deploy.yml` to the new name)

**Interfaces:**
- Produces: a deploy that checks out the renamed adapter repo into `adapters/odoo-nil-adapter/` and installs it.

- [ ] **Step 1: Update the deploy workflow**

In `nilscript-landing/.github/workflows/deploy.yml`, change:
```yaml
          repository: nilscript-org/odoo-nil-adapter
          path: adapters/odoo-nil-adapter
```

- [ ] **Step 2: Update the playground Dockerfile**

In `nilscript-landing/deploy/playground.Dockerfile`:
```dockerfile
COPY adapters/odoo-nil-adapter/ /app/odoo-nil-adapter/
# ...
RUN pip install --no-cache-dir -e "/app/nilscript[demo]" -e "/app/odoo-nil-adapter"
```

- [ ] **Step 3: Remove the now-redundant staged grant (host)**

```bash
ssh root@77.42.70.107 'cd /root/nilscript-landing && \
  sed -i "/^NIL_TENANT_GRANTS=/d" .env && \
  sed -i "/NIL_TENANT_GRANTS=\${NIL_TENANT_GRANTS:-}/d" docker-compose.prod.yml && \
  grep -c NIL_TENANT_GRANTS .env docker-compose.prod.yml || echo "grant removed"'
```
Expected: `grant removed` (no matches). Purchasing now ships in the default skeleton, so the grant is unnecessary.

- [ ] **Step 4: Commit deploy changes (in nilscript-landing repo)**

```bash
cd /home/ubuntu/Downloads/nizam/nilscript-landing
git add .github/workflows/deploy.yml deploy/playground.Dockerfile
git commit -m "deploy: point playground at renamed odoo-nil-adapter; drop staged PO grant"
```

---

### Task 6: Rebuild + redeploy playground image; live verification

**Files:** none (operational).

- [ ] **Step 1: Push adapter + landing changes**

Push the adapter repo (renamed) and the `nilscript-landing` deploy changes so CI can build. Confirm the GitHub repo rename redirect resolves for the `deploy.yml` checkout.

- [ ] **Step 2: Rebuild and redeploy the playground image on the host**

```bash
ssh root@77.42.70.107 'cd /root/nilscript-landing && \
  docker compose -f docker-compose.prod.yml pull playground || true && \
  docker compose -f docker-compose.prod.yml up -d --build playground 2>&1 | tail -6'
```

- [ ] **Step 3: Verify the deployed adapter now advertises purchasing**

```bash
ssh root@77.42.70.107 'docker exec nilscript-playground sh -lc "ls /app/odoo-nil-adapter/src/odoo_nil_adapter/packs.py && python -c \"from odoo_nil_adapter import governance as g; print(g.write_tier(\\\"purchase.order\\\",\\\"create\\\")); print(g.method_reverse(\\\"purchase.order\\\",\\\"button_confirm\\\"))\""'
```
Expected: `HIGH` then `button_cancel`.

- [ ] **Step 4: Verify a PO create PROPOSES (no side effect) end-to-end**

Through the MCP front door (`ws_acme`), call `nil_describe` and confirm `purchase.order` appears in writable targets, then `nil_propose` a `purchase.create_order` and confirm it returns a PROPOSAL (not a refusal). Do NOT commit — propose only.

- [ ] **Step 5: Update memory**

Record: production adapter renamed to `odoo-nil-adapter`, purchasing pack live (PO create + compensable confirm), staged grant removed, module-pack architecture in `packs.py`.

---

## Self-Review

**Spec coverage:**
- Pack registry (thin dataclass) → Task 1. ✓
- crm/finance/sales/inventory migrated verbatim → Tasks 1–2. ✓
- Purchasing pack (create + compensable confirm, `purchase.`/`uom.` group) → Task 3. ✓
- All-packs-on default (`_ENABLED_MODULES=None`) → preserved (Task 2 keeps `module_enabled` default). ✓
- Write-ceiling invariant / `test_unexpressibility` green → Task 2 note + Task 3 tests. ✓
- Rename package/repo/deploy path → Tasks 4–5. ✓
- Remove redundant staged grant → Task 5 Step 3. ✓
- Rebuild+redeploy → Task 6. ✓

**Placeholder scan:** No TBD/TODO; new code shown in full; verbatim migrations given as exact mechanical recipes (move existing objects to constants) rather than re-pasting 600 unchanged lines.

**Type consistency:** `ModulePack` fields, `all_write_verbs()/all_query_verbs()/all_write_targets()/module_models()/default_methods()/all_projections()/all_sensitive()` names are consistent across Tasks 1–3. Verb constant names (`PURCHASE_CREATE_ORDER`, `PURCHASE_CONFIRM_ORDER`) match between translate.py and packs.py. `reverse_method="button_cancel"` consistent between verb and pack `method_grants`.

**Open risk to watch during execution:** the `translate ↔ packs` import order. `packs.py` imports `WriteVerb`/`QueryVerb` from `translate.py`, and `translate.py` late-imports `packs` at the bottom (after verb constants are defined). If a cycle bites, move the `WriteVerb`/`QueryVerb` dataclasses into a tiny `verbs.py` both import — note this in Task 1.
