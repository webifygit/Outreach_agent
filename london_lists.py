"""What captcha_assist.py and captcha_helper.py still need from the London scraper.

scrape_london_hotels.py is no longer in this folder, and neither are its search
and place files, so the campaigns built on them have nothing new to offer.
"""
import csv
import os
import re
from urllib.parse import urlsplit

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "london_hotels_output")
FORMS = os.path.join(OUT, "forms.csv")
LODGING, FOOD, CARE = set(), set(), set()
NEVER = re.compile(r"$^")
CARE_CATEGORY_RE = NOT_OWN_SITE = NEVER


def host_of(url: str) -> str:
    h = urlsplit(url).netloc.split("@")[-1].split(":")[0].lower()
    return h[4:] if h.startswith("www.") else h


def read_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def london_hosts(queries) -> set[str]:
    return set()


def hosts_for(queries) -> set[str]:
    return set()


def lodging_hosts() -> set[str]:
    return set()


def unique_sites(*_args, **_kwargs) -> list[dict]:
    return []
