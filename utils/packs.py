import random
from dataclasses import dataclass

from utils import database

RARITY_EMOJI = {
    "common": "⚪",
    "uncommon": "🔵",
    "rare": "🟡",
    "mythic": "🟠",
}

BOOSTER_LABELS = {
    "play": "Play Booster",
    "collector": "Collector Booster",
}

# --- Play Booster: 15 cards -------------------------------------------------
# 14 independently rolled by rarity, + 1 basic land. Of the 14: 2 (picked at
# random) are guaranteed foil if that printing supports foil, and a
# different 2 each get a chance to be upgraded to a special-treatment
# printing of that same card, if one exists in the set.
PLAY_RARITY_WEIGHTS = {"mythic": 1, "rare": 10, "uncommon": 20, "common": 69}
PLAY_CARD_COUNT = 14
PLAY_GUARANTEED_FOILS = 2
PLAY_SPECIAL_ELIGIBLE = 2
PLAY_SPECIAL_CHANCE = 0.20
PLAY_LAND_FOIL_CHANCE = 0.20

# --- Collector Booster: 15 cards --------------------------------------------
# 14 independently rolled by rarity (richer distribution than Play), + 1
# land (foil, prefers full-art). Every one of the 14 is foil whenever the
# printing supports it. 4 of the 14 each get a chance at a special-treatment
# upgrade. Each of the 14 also independently has a chance to be swapped for
# a card from the set's bonus-linked Commander deck instead.
COLLECTOR_RARITY_WEIGHTS = {"mythic": 10, "rare": 20, "uncommon": 30, "common": 40}
COLLECTOR_CARD_COUNT = 14
COLLECTOR_SPECIAL_ELIGIBLE = 4
COLLECTOR_SPECIAL_CHANCE = 0.30
COLLECTOR_BONUS_CHANCE = 0.20


@dataclass
class PulledCard:
    card_id: str
    name: str
    rarity: str
    image_url: str | None
    foil: bool = False
    special: bool = False  # extended art / showcase treatment
    bonus: bool = False  # from a bonus-linked Commander deck, not the base set


def _weighted_rarity(weights: dict[str, int]) -> str:
    return random.choices(list(weights.keys()), weights=list(weights.values()))[0]


def _can_be_foil(finishes) -> bool:
    return "foil" in (finishes or [])


async def _draw_card(
    set_code: str,
    rarity: str,
    *,
    foil_chance: float = 0.0,
    special_chance: float = 0.0,
    bonus: bool = False,
) -> PulledCard | None:
    """Draw one base-version card, maybe upgrade it to a special-treatment
    printing of the same card, then decide foil based on whatever printing
    it ends up as. Basic lands are excluded — they only ever come from the
    pack's dedicated guaranteed land slot, never one of the regular cards."""
    rows = await database.get_random_cards(
        set_code, rarity, 1, exclude_basic_land=True, exclude_special_treatment=True
    )
    if not rows:
        return None
    row = rows[0]
    card_id, oracle_id, name, drawn_rarity, image_url, finishes = (
        row["id"],
        row["oracle_id"],
        row["name"],
        row["rarity"],
        row["image_url"],
        row["finishes"],
    )
    special = False

    if special_chance > 0 and random.random() < special_chance:
        alt = await database.get_special_treatment_printing(oracle_id, set_code)
        if alt:
            card_id, name, drawn_rarity, image_url, finishes = (
                alt["id"],
                alt["name"],
                alt["rarity"],
                alt["image_url"],
                alt["finishes"],
            )
            special = True

    foil = _can_be_foil(finishes) and random.random() < foil_chance
    return PulledCard(str(card_id), name, drawn_rarity, image_url, foil, special, bonus)


async def _draw_land(set_code: str, *, foil_chance: float) -> PulledCard | None:
    rows = await database.get_random_basic_lands(set_code, 1)
    if not rows:
        return None
    row = rows[0]
    foil = _can_be_foil(row["finishes"]) and random.random() < foil_chance
    return PulledCard(str(row["id"]), row["name"], row["rarity"], row["image_url"], foil)


async def _open_play_booster(set_code: str) -> list[PulledCard]:
    foil_indices = set(random.sample(range(PLAY_CARD_COUNT), PLAY_GUARANTEED_FOILS))
    special_indices = set(random.sample(range(PLAY_CARD_COUNT), PLAY_SPECIAL_ELIGIBLE))

    pulled: list[PulledCard] = []
    for i in range(PLAY_CARD_COUNT):
        rarity = _weighted_rarity(PLAY_RARITY_WEIGHTS)
        card = await _draw_card(
            set_code,
            rarity,
            foil_chance=1.0 if i in foil_indices else 0.0,
            special_chance=PLAY_SPECIAL_CHANCE if i in special_indices else 0.0,
        )
        if card:
            pulled.append(card)

    land = await _draw_land(set_code, foil_chance=PLAY_LAND_FOIL_CHANCE)
    if land:
        pulled.append(land)

    return pulled


async def _open_collector_booster(set_code: str, bonus_set_code: str | None) -> list[PulledCard]:
    special_indices = set(random.sample(range(COLLECTOR_CARD_COUNT), COLLECTOR_SPECIAL_ELIGIBLE))

    pulled: list[PulledCard] = []
    for i in range(COLLECTOR_CARD_COUNT):
        rarity = _weighted_rarity(COLLECTOR_RARITY_WEIGHTS)
        use_bonus = bool(bonus_set_code) and random.random() < COLLECTOR_BONUS_CHANCE
        source_set = bonus_set_code if use_bonus else set_code
        card = await _draw_card(
            source_set,
            rarity,
            foil_chance=1.0,
            special_chance=COLLECTOR_SPECIAL_CHANCE if i in special_indices else 0.0,
            bonus=use_bonus,
        )
        if card:
            pulled.append(card)

    land = await _draw_land(set_code, foil_chance=1.0)
    if land:
        pulled.append(land)

    return pulled


async def open_pack(guild_id: int, user_id: int, set_code: str, booster_type: str = "play") -> list[PulledCard]:
    if booster_type == "play":
        pulled = await _open_play_booster(set_code)
    elif booster_type == "collector":
        bonus_set_code = await database.get_bonus_set_code(set_code)
        pulled = await _open_collector_booster(set_code, bonus_set_code)
    else:
        raise ValueError(f"Unknown booster_type '{booster_type}'; expected 'play' or 'collector'")

    await database.add_to_collection(guild_id, user_id, [card.card_id for card in pulled])
    return pulled
