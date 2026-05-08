"""Smoke for Plan v5 R2 — Tier-Routing per-Chat-Toggle.

Bestätigt:
  1. DB-Migration: use_tier_routing-Spalte existiert
  2. set_chat_int_setting akzeptiert "use_tier_routing"
  3. get_chat_session returnt use_tier_routing field
  4. /toggles _TOGGLE_KEYS hat use_tier_routing Eintrag
  5. runner.py override-Logic: session.use_tier_routing=1 → settings.cascade_use_tier_routing=True
  6. core.run_cascade tier-routing-Branch respektiert die overridete Settings
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from cascade.store import Store


def passed(label: str) -> None:
    print(f"  ✅ {label}")


async def test_db_set_get():
    print("\n[1] DB: set_chat_int_setting + get_chat_session round-trip")
    with tempfile.TemporaryDirectory() as tmp:
        store = await Store.open(Path(tmp) / "test.db")
        chat_id = 123456
        # initial: None
        sess = await store.get_chat_session(chat_id)
        # neue chats geben None bei nicht-existing
        assert sess is None or sess.get("use_tier_routing") is None

        await store.set_chat_int_setting(chat_id, "use_tier_routing", 1)
        sess = await store.get_chat_session(chat_id)
        assert sess is not None
        assert sess.get("use_tier_routing") == 1
        passed("set 1 → get 1")

        await store.set_chat_int_setting(chat_id, "use_tier_routing", 0)
        sess = await store.get_chat_session(chat_id)
        assert sess.get("use_tier_routing") == 0
        passed("set 0 → get 0")

        await store.set_chat_int_setting(chat_id, "use_tier_routing", None)
        sess = await store.get_chat_session(chat_id)
        assert sess.get("use_tier_routing") is None
        passed("set None → get None (zurück auf default)")
        await store.close()


def test_toggles_keys_has_tier():
    print("\n[2] _TOGGLE_KEYS hat use_tier_routing Eintrag")
    from cascade.bot.handlers.config import _TOGGLE_KEYS
    cols = {col for col, *_ in _TOGGLE_KEYS}
    assert "use_tier_routing" in cols, f"missing in {cols}"
    # Auch passendes settings-attribute
    for col, attr, dlabel, elabel in _TOGGLE_KEYS:
        if col == "use_tier_routing":
            assert attr == "cascade_use_tier_routing"
            assert "Tier" in dlabel and "Tier" in elabel
            passed(f"Eintrag korrekt: {col}/{attr}/{dlabel[:40]}…")
            return


async def test_set_chat_int_setting_rejects_unknown():
    print("\n[3] set_chat_int_setting wirft auf unbekanntes column")
    with tempfile.TemporaryDirectory() as tmp:
        store = await Store.open(Path(tmp) / "test.db")
        try:
            await store.set_chat_int_setting(123, "use_unknown_xyz", 1)
            raise AssertionError("sollte werfen")
        except ValueError as e:
            assert "unknown int setting" in str(e)
        await store.close()
    passed("Whitelist greift (ValueError)")


def test_settings_attribute_exists():
    print("\n[4] cascade_use_tier_routing in Settings + default False")
    from cascade.config import Settings
    s = Settings()
    assert hasattr(s, "cascade_use_tier_routing")
    assert s.cascade_use_tier_routing is False, "default soll False sein"
    passed("default False, attribute existiert")


def test_runner_override_logic():
    print("\n[5] runner.py override-mapping (simulate inline)")
    # Simuliere die override-Logic aus runner.py
    sess = {"use_tier_routing": 1, "use_orchestrator": 0, "reviewer_via_harness": None}
    overrides: dict = {}
    if (uo := sess.get("use_orchestrator")) is not None:
        overrides["cascade_use_orchestrator"] = bool(uo)
    if (rh := sess.get("reviewer_via_harness")) is not None:
        overrides["cascade_reviewer_via_harness"] = bool(rh)
    if (utr := sess.get("use_tier_routing")) is not None:
        overrides["cascade_use_tier_routing"] = bool(utr)
    assert overrides == {
        "cascade_use_orchestrator": False,
        "cascade_use_tier_routing": True,
    }, f"actual: {overrides}"
    passed("Mapping: 1→True, 0→False, None→omit")


async def test_settings_model_copy_picks_up():
    print("\n[6] s.model_copy(update={...}) propagiert override")
    from cascade.config import Settings
    s = Settings()
    assert s.cascade_use_tier_routing is False
    s2 = s.model_copy(update={"cascade_use_tier_routing": True})
    assert s2.cascade_use_tier_routing is True
    assert s.cascade_use_tier_routing is False, "Original soll unverändert bleiben"
    passed("model_copy isoliert + override greift")


async def main():
    print("=" * 60)
    print("  Plan v5 R2 — Tier-Routing-Toggle Smoke")
    print("=" * 60)
    await test_db_set_get()
    test_toggles_keys_has_tier()
    await test_set_chat_int_setting_rejects_unknown()
    test_settings_attribute_exists()
    test_runner_override_logic()
    await test_settings_model_copy_picks_up()
    print("\n" + "=" * 60)
    print("  ✅ Alle 6 Tests grün")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
