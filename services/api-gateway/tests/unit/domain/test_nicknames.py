"""The nickname blocklist (A-01, projector safety): what is refused, and what must never be."""

import pytest

from api_gateway.domain.nicknames import NicknameBlocklist, normalise

BLOCKLIST = NicknameBlocklist.default()

OBVIOUS = [
    "fuck",
    "FuckYou",
    "bitch",
    "asshole",
    "nigger",
    "madarchod",
    "Behenchod",
    "Chutiya",
    "bhosdike",
    "gandu",
    "randi",
    "Lauda",
    "harami",
    "kamina",
]

LEETSPEAK = [
    "sh1t",  # 1 → i
    "b1tch",
    "ch00tiya",  # 0 → o, then oo → o
    "r4nd1",  # 4 → a, 1 → i
    "g4ndu",
    "wh0r3",  # 3 → e
    "5lut",  # 5 → s
    "7wat",  # 7 → t
    "$hit",  # $ → s (A-01's pattern already refuses $ and @; the rule still maps them)
    "@sshole",  # @ → a
]

PADDED = [
    "f_u_c_k",  # underscores removed
    "fuuuuuck",  # repeats collapsed
    "BITCHHHH",
    "gaaaandu",
    "x_madarchod_x",
    "Bhos_d1ke99",  # digits removed after the leetspeak map
    "chu_ti_ya",
    "RaNdI_123",
    "1fuck2",
    "Harshit_Shit",  # an ordinary name around it does not excuse it
    "NigamNigger",
    "Ishita_Bitch",  # an exception covers only the word it is inside
]

# Ordinary names (mostly Indian, as the audience will be) and words that contain a short blocked
# word, or something that looks like one after normalising. None of these may be refused.
FALSE_POSITIVES = [
    "Gandhi",  # gand(u)
    "Harshit",  # shit
    "Harshita",
    "Darshit",
    "Ashit",
    "Ashita",
    "Ishita",
    "Nishita",
    "Rishit",
    "Akshita",
    "Lakshit",
    "Kshitij",
    "Shitij",
    "Dikshit",
    "Shital",
    "Sheetal",
    "Shitanshu",
    "Nigam",  # nigga → niga
    "Nigar",
    "Nazim",  # nazi
    "Nazia",
    "Nazir",
    "Nazish",
    "Randip",  # randi
    "Randeep",
    "Randhir",
    "Pornima",  # porn
    "Lodaya",  # loda
    "Kamini",  # kamina
    "Kaminee",
    "Hassan",  # ass (not on the list)
    "Bassi",
    "Prasad",
    "Sushant",
    "Ganesh",
    "Shankar",
    "Anand",
    "Rakesh",
    "Chutney_Lover",  # chut (not on the list)
    "Dickson",  # dick
    "PeacockFan",  # cock
    "Grapes",  # rape
    "Therapist",  # rapist
    "Plunder",  # lund
    "Pusya",  # pussy → pusy
    "RajuTrader",
    "MeeraMoon",
]


@pytest.mark.parametrize("nickname", OBVIOUS + LEETSPEAK + PADDED)
def test_offensive_nicknames_are_refused(nickname: str) -> None:
    assert BLOCKLIST.refuses(nickname), nickname


@pytest.mark.parametrize("nickname", FALSE_POSITIVES)
def test_ordinary_names_with_a_short_lookalike_inside_are_accepted(nickname: str) -> None:
    assert not BLOCKLIST.refuses(nickname), nickname


def test_normalising_lowercases_maps_leetspeak_strips_and_collapses() -> None:
    assert normalise("F_u_U_c_K") == "fuck"
    assert normalise("0134@$57") == "oieast"  # o i e a a s s t, then aa → a and ss → s
    assert normalise("x2y6z8w9") == "xyzw"  # digits without a letter meaning are dropped
    assert normalise("Gaaandu___") == "gandu"


def test_the_list_has_english_and_hindi_words_and_no_entry_shorter_than_four_letters() -> None:
    assert {"fuck", "bitch", "madarchod", "chutiya", "bhosdike"} <= set(BLOCKLIST.blocked)
    assert min(len(w) for w in BLOCKLIST.blocked) >= 4


def test_a_list_is_read_from_its_sections_and_an_entry_outside_one_is_an_error() -> None:
    small = NicknameBlocklist.from_text(
        "# comment\n[blocked]\nbadword\n\n[allowed]\nbadwordsmith\n"
    )
    assert small.refuses("bad_w0rd") and not small.refuses("BadwordSmith")
    with pytest.raises(ValueError, match="outside a section"):
        NicknameBlocklist.from_text("badword\n")
