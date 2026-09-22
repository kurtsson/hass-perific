"""Generating a dashboard for series that have no entity.

The statistics this integration writes are named after the meter's own id, so a
dashboard for them cannot be shipped as a file — every install needs different
ids. This builds one from what the entry already knows, and hands it back as
YAML for the user to paste.

Nothing here touches the user's dashboards; Home Assistant offers no supported
way for an integration to add one, and doing it through the private internals
would fight whatever the user edits afterwards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .const import (
    SOLAR_AVOIDED_COST,
    SOLAR_REVENUE,
    SOLAR_SELF_CONSUMED,
)
from .history import statistic_id

if TYPE_CHECKING:
    from .api import Item

# Card names come from the integration's own translations, but a section
# heading belongs to no entity and so has nothing to read. Home Assistant
# supports only its own fixed translation categories, so the few strings that
# are ours alone live here, falling back to English.
HEADINGS: dict[str, dict[str, str]] = {
    "en": {
        "title": "Solar economy",
        "money": "This month",
        "energy": "Energy this month",
        "money_daily": "What the panels gave, per day",
        "energy_daily": "Used directly against sold",
        "produced": "Produced",
    },
    "sv": {
        "title": "Solel",
        "money": "Den här månaden",
        "energy": "Energi den här månaden",
        "money_daily": "Vad panelerna gett per dag",
        "energy_daily": "Egenanvänd solel mot såld",
        "produced": "Producerat",
    },
}


def _heading(language: str, key: str) -> str:
    return HEADINGS.get(language, HEADINGS["en"]).get(key, HEADINGS["en"][key])


def _statistic_card(entity: str, name: str) -> dict[str, Any]:
    """One figure, aggregated over the current calendar month.

    ``change`` rather than ``sum``: these series are cumulative totals, so their
    sum is whatever the meter has counted since it was installed.
    """
    return {
        "type": "statistic",
        "entity": entity,
        "stat_type": "change",
        "period": {"calendar": {"period": "month"}},
        "name": name,
    }


def _graph_card(entities: list[str]) -> dict[str, Any]:
    """Chart what each series contributed, per day.

    The key is ``entities`` even though these are statistic ids and not
    entities; the card takes either, and rejects ``statistic_ids`` outright.
    """
    return {
        "type": "statistics-graph",
        "entities": entities,
        "stat_types": ["change"],
        "chart_type": "bar",
        "period": "day",
        "days_to_show": 30,
    }


def _prefixed(meters: list[Item], meter: Item, name: str) -> str:
    """Name a card, disambiguating only when there is more than one meter."""
    if len(meters) < 2:  # noqa: PLR2004
        return name
    return f"{meter.name or meter.system_name or meter.item_id} {name}"


def view_config(
    meters: list[Item],
    names: dict[str, str],
    language: str,
    *,
    solar_statistic: str | None,
    cost: bool,
) -> dict[str, Any]:
    """Build the view, carrying only the sections the entry can fill.

    A card pointing at a statistic that is never written shows as unavailable
    forever, so cost and solar are left out entirely unless configured.
    """
    money: list[dict[str, Any]] = []
    energy: list[dict[str, Any]] = []
    money_daily: list[str] = []
    energy_daily: list[str] = []

    for meter in meters:
        item = meter.item_id

        def card(key: str, meter: Item = meter) -> dict[str, Any]:
            return _statistic_card(
                statistic_id(meter.item_id, key), _prefixed(meters, meter, names[key])
            )

        if solar_statistic and cost:
            money.append(card(SOLAR_REVENUE))
            money.append(card(SOLAR_AVOIDED_COST))
            money_daily.append(statistic_id(item, SOLAR_AVOIDED_COST))
        if cost:
            money.append(card("energy_export_compensation"))
            money.append(card("energy_import_cost"))
            money_daily.append(statistic_id(item, "energy_export_compensation"))
        if solar_statistic:
            energy.append(card(SOLAR_SELF_CONSUMED))
            energy_daily.append(statistic_id(item, SOLAR_SELF_CONSUMED))
        energy.append(card("energy_export"))
        energy.append(card("energy_import"))
        energy_daily.append(statistic_id(item, "energy_export"))

    if solar_statistic:
        # Once, not per meter: the panels belong to the house, while the
        # statistics above belong to each meter.
        energy.insert(
            0, _statistic_card(solar_statistic, _heading(language, "produced"))
        )

    sections: list[dict[str, Any]] = []
    if money:
        sections.append(_section(_heading(language, "money"), money))
    sections.append(_section(_heading(language, "energy"), energy))

    graphs: list[dict[str, Any]] = []
    if money_daily:
        graphs.append(_headline(_heading(language, "money_daily")))
        graphs.append(_graph_card(money_daily))
    graphs.append(_headline(_heading(language, "energy_daily")))
    graphs.append(_graph_card(energy_daily))
    sections.append({"type": "grid", "column_span": 2, "cards": graphs})

    return {
        "type": "sections",
        "title": _heading(language, "title"),
        "path": "perific-solar",
        "icon": "mdi:solar-power",
        "max_columns": 2,
        "sections": sections,
    }


def _headline(heading: str) -> dict[str, Any]:
    return {"type": "heading", "heading": heading, "heading_style": "title"}


def _section(heading: str, cards: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "grid", "cards": [_headline(heading), *cards]}


def dashboard_config(
    meters: list[Item],
    names: dict[str, str],
    language: str,
    *,
    solar_statistic: str | None,
    cost: bool,
) -> dict[str, Any]:
    """Wrap the view as a whole dashboard, which the raw editor expects."""
    return {
        "views": [
            view_config(
                meters, names, language, solar_statistic=solar_statistic, cost=cost
            )
        ]
    }
