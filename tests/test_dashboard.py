"""Generating a dashboard for series that have no entity.

The statistic ids carry the meter's own id, so the generated config is the only
thing a second install could use — a file could only ever be right for one
household. These assert the shape Home Assistant's YAML editors accept, and
that nothing points at a series this instance does not write.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import yaml

from custom_components.perific.api import Item
from custom_components.perific.const import (
    COST_NAMES,
    DOMAIN,
    HISTORY_NAMES,
    SERVICE_GET_DASHBOARD,
    SOLAR_NAMES,
    SOLAR_SELF_CONSUMED,
)
from custom_components.perific.dashboard import dashboard_config, view_config

if TYPE_CHECKING:
    from collections.abc import Iterator

SOLAR = "solaredge:4064370_7b03cd72_bd"
NAMES = HISTORY_NAMES | COST_NAMES | SOLAR_NAMES


def meter(item_id: int, name: str = "OneRJ12") -> Item:
    return Item(item_id=item_id, name=name, system_name=name, time_zone=None)


def cards(config: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every card in a view, whichever section it sits in."""
    for section in config["sections"]:
        yield from section["cards"]


def referenced(config: dict[str, Any]) -> set[str]:
    """Every statistic id the view points at."""
    found: set[str] = set()
    for card in cards(config):
        if entity := card.get("entity"):
            found.add(entity)
        found.update(card.get("entities") or [])
    return found


class TestSections:
    """Only what the entry can actually fill."""

    def test_energy_only_when_nothing_is_configured(self) -> None:
        view = view_config([meter(1)], NAMES, "en", solar_statistic=None, cost=False)
        assert referenced(view) == {
            "perific:1_energy_import",
            "perific:1_energy_export",
        }

    def test_cost_appears_with_a_price_entity(self) -> None:
        view = view_config([meter(1)], NAMES, "en", solar_statistic=None, cost=True)
        assert "perific:1_energy_import_cost" in referenced(view)
        assert "perific:1_solar_revenue" not in referenced(view)

    def test_solar_needs_a_price_too(self) -> None:
        """Valuing the solar is what the price is for; without it there is no sum."""
        view = view_config([meter(1)], NAMES, "en", solar_statistic=SOLAR, cost=False)
        assert "perific:1_solar_revenue" not in referenced(view)
        assert "perific:1_solar_self_consumed" in referenced(view)

    def test_everything_configured_names_every_series(self) -> None:
        view = view_config([meter(1)], NAMES, "en", solar_statistic=SOLAR, cost=True)
        assert referenced(view) == {
            SOLAR,
            "perific:1_energy_import",
            "perific:1_energy_export",
            "perific:1_energy_import_cost",
            "perific:1_energy_export_compensation",
            "perific:1_solar_self_consumed",
            "perific:1_solar_avoided_cost",
            "perific:1_solar_revenue",
        }

    def test_a_card_never_points_at_an_unwritten_series(self) -> None:
        """An unavailable card is indistinguishable from a broken integration."""
        for solar in (None, SOLAR):
            for cost in (False, True):
                view = view_config(
                    [meter(1)], NAMES, "en", solar_statistic=solar, cost=cost
                )
                ids = referenced(view) - {SOLAR}
                if not cost:
                    assert not [i for i in ids if "cost" in i or "compensation" in i]
                if solar is None:
                    assert not [i for i in ids if "solar" in i]


class TestNaming:
    """Cards are named from the same translations the statistics carry."""

    def test_uses_the_translated_names(self) -> None:
        names = NAMES | {"energy_import": "Inköpt elektricitet"}
        view = view_config([meter(1)], names, "sv", solar_statistic=None, cost=False)
        assert "Inköpt elektricitet" in [card.get("name") for card in cards(view)]

    def test_headings_follow_the_instance_language(self) -> None:
        assert (
            view_config([meter(1)], NAMES, "sv", solar_statistic=None, cost=False)[
                "title"
            ]
            == "Solel"
        )

    def test_an_unknown_language_falls_back_to_english(self) -> None:
        assert (
            view_config([meter(1)], NAMES, "fr", solar_statistic=None, cost=False)[
                "title"
            ]
            == "Solar economy"
        )

    def test_one_meter_is_not_prefixed(self) -> None:
        view = view_config([meter(1)], NAMES, "en", solar_statistic=None, cost=False)
        assert "Imported electricity" in [card.get("name") for card in cards(view)]

    def test_production_is_named_for_itself(self) -> None:
        """It is another integration's series, so our own names do not describe it."""
        view = view_config([meter(1)], NAMES, "en", solar_statistic=SOLAR, cost=True)
        named = [card.get("name") for card in cards(view)]
        assert "Produced" in named
        assert named.count(NAMES[SOLAR_SELF_CONSUMED]) == 1

    def test_production_is_shown_once_for_several_meters(self) -> None:
        """The panels belong to the house, not to either meter."""
        view = view_config(
            [meter(1, "Garage"), meter(2, "House")],
            NAMES,
            "en",
            solar_statistic=SOLAR,
            cost=True,
        )
        assert [c.get("entity") for c in cards(view)].count(SOLAR) == 1

    def test_several_meters_are_told_apart(self) -> None:
        """Two meters otherwise produce two identically named cards."""
        view = view_config(
            [meter(1, "Garage"), meter(2, "House")],
            NAMES,
            "en",
            solar_statistic=None,
            cost=False,
        )
        named = [card.get("name") for card in cards(view)]
        assert "Garage Imported electricity" in named
        assert "House Imported electricity" in named
        assert referenced(view) >= {
            "perific:1_energy_import",
            "perific:2_energy_import",
        }


class TestYamlShape:
    """What the two editors in Home Assistant actually accept."""

    def test_the_dashboard_form_has_a_views_list(self) -> None:
        """The raw editor rejects anything else: 'Expected an array value'."""
        config = dashboard_config(
            [meter(1)], NAMES, "en", solar_statistic=None, cost=True
        )
        assert isinstance(config["views"], list)
        assert config["views"][0]["type"] == "sections"

    def test_graphs_use_entities_not_statistic_ids(self) -> None:
        """``statistic_ids`` is refused with 'Entities need to be an array'."""
        view = view_config([meter(1)], NAMES, "en", solar_statistic=SOLAR, cost=True)
        graphs = [c for c in cards(view) if c["type"] == "statistics-graph"]
        assert graphs
        for graph in graphs:
            assert isinstance(graph["entities"], list)
            assert "statistic_ids" not in graph

    def test_money_is_a_change_not_a_running_total(self) -> None:
        """These series accumulate forever; their sum is meaningless on a card."""
        view = view_config([meter(1)], NAMES, "en", solar_statistic=SOLAR, cost=True)
        for card in cards(view):
            if card["type"] == "statistic":
                assert card["stat_type"] == "change"

    def test_it_round_trips_through_yaml(self) -> None:
        config = dashboard_config(
            [meter(1)], NAMES, "sv", solar_statistic=SOLAR, cost=True
        )
        assert yaml.safe_load(yaml.safe_dump(config, allow_unicode=True)) == config


@pytest.mark.usefixtures("recorder_mock")
class TestService:
    """``perific.get_dashboard``."""

    async def test_returns_both_forms(self, hass: Any, setup_integration: Any) -> None:
        response = await hass.services.async_call(
            DOMAIN, SERVICE_GET_DASHBOARD, {}, blocking=True, return_response=True
        )

        assert response is not None
        dashboard = yaml.safe_load(response["dashboard"])
        view = yaml.safe_load(response["view"])
        assert dashboard["views"][0] == view
        assert view["type"] == "sections"
