"""Heuristic contact-name extraction from email local-part.

Goal: turn `mary@bobsdiner.com` into "Mary" — never "team", never the company name,
and never a fake name like "Ckamats" from `ckamats@` or "Bobsdiner" from `bobsdiner@`.

Rule:
  - Multi-segment locals ("mary.smith", "john_doe") → trust the first segment as a name.
  - Single-token locals ("ckamats", "bobsdiner", "mary") → only accept when the token
    is a recognized first name (COMMON_FIRST_NAMES). Otherwise return None and let the
    caller fall back to FALLBACK_NAME ("team").

Returns None for generic role mailboxes, numeric/short locals, or low-confidence cases.
"""
import re

GENERIC_LOCAL_PARTS = frozenset({
    "info", "contact", "hello", "hi", "office", "admin", "support",
    "sales", "service", "help", "team", "business", "mail", "email",
    "inquiries", "inquiry", "general", "main", "front", "frontdesk",
    "manager", "owner", "store", "shop", "salon", "studio", "garage",
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "marketing", "press", "media", "pr", "hr", "jobs", "careers",
    "billing", "accounts", "accounting", "ar", "ap", "finance",
    "appointments", "booking", "reservations", "orders",
    "feedback", "comments", "questions", "ask",
})

# Common US/anglophone first names. Used to validate single-token local-parts so a
# company-derived local ("bobsdiner") or a last-name login ("ckamats") never becomes
# a fake greeting. Multi-segment locals bypass this list (first segment is a name).
COMMON_FIRST_NAMES = frozenset({
    "aaron", "adam", "adrian", "alan", "albert", "alex", "alexander", "alexis",
    "alice", "alicia", "allison", "amanda", "amber", "amy", "andrea", "andrew",
    "angela", "ann", "anna", "anne", "anthony", "antonio", "april", "arthur",
    "ashley", "austin", "barbara", "ben", "benjamin", "beth", "bill", "billy",
    "bob", "bobby", "brad", "bradley", "brandon", "brenda", "brian", "brittany",
    "bruce", "bryan", "caleb", "cameron", "carl", "carlos", "carol", "caroline",
    "carrie", "casey", "catherine", "chad", "charles", "charlie", "charlotte",
    "chase", "chelsea", "cheryl", "chris", "christian", "christina", "christine",
    "christopher", "cindy", "claire", "clara", "cody", "colin", "connor",
    "courtney", "craig", "crystal", "curtis", "cynthia", "dale", "dan", "daniel",
    "danielle", "danny", "darlene", "dave", "david", "dawn", "dean", "deborah",
    "debra", "denise", "dennis", "derek", "diana", "diane", "dominic", "don",
    "donald", "donna", "doris", "dorothy", "douglas", "dylan", "earl", "ed",
    "eddie", "edgar", "edward", "edwin", "eileen", "elaine", "eleanor", "elena",
    "eli", "elijah", "elizabeth", "ellen", "emily", "emma", "eric", "erica",
    "erik", "erin", "ethan", "eugene", "evan", "evelyn", "faith", "felicia",
    "fernando", "frances", "francis", "frank", "fred", "gabriel", "gail",
    "gary", "gavin", "gene", "george", "gerald", "gina", "glen", "glenn",
    "gloria", "gordon", "grace", "greg", "gregory", "hannah", "harold", "harry",
    "heather", "hector", "helen", "henry", "holly", "howard", "ian", "isaac",
    "isabella", "jack", "jackie", "jacob", "jacqueline", "jaime", "jake",
    "james", "jamie", "jane", "janet", "janice", "jared", "jason", "jay",
    "jean", "jeff", "jeffery", "jeffrey", "jennifer", "jenny", "jeremy",
    "jerry", "jesse", "jessica", "jill", "jim", "jimmy", "joan", "joann",
    "joanne", "joe", "joel", "john", "johnny", "jon", "jonathan", "jordan",
    "jose", "joseph", "josh", "joshua", "joyce", "juan", "judith", "judy",
    "julia", "julian", "julie", "justin", "kaitlyn", "karen", "kate",
    "katherine", "kathleen", "kathryn", "kathy", "katie", "kayla", "keith",
    "kelly", "ken", "kenneth", "kevin", "kim", "kimberly", "kristen", "kristin",
    "kyle", "larry", "laura", "lauren", "laurie", "lawrence", "lee", "leo",
    "leonard", "leslie", "levi", "linda", "lindsay", "lindsey", "lisa", "logan",
    "lois", "loretta", "lori", "louis", "lucas", "lucy", "luis", "luke",
    "lynn", "madison", "marcus", "margaret", "maria", "marie", "marilyn",
    "mario", "marion", "mark", "marsha", "martha", "martin", "marvin", "mary",
    "mason", "matt", "matthew", "maureen", "megan", "melanie", "melissa",
    "michael", "michele", "michelle", "mike", "mitchell", "molly", "monica",
    "morgan", "nancy", "natalie", "nathan", "neil", "nicholas", "nick",
    "nicole", "nina", "noah", "nora", "norman", "olivia", "oscar", "owen",
    "pam", "pamela", "pat", "patricia", "patrick", "paul", "paula", "pedro",
    "peggy", "peter", "philip", "phillip", "phyllis", "rachel", "ralph",
    "randall", "randy", "ray", "raymond", "rebecca", "regina", "renee",
    "rex", "ricardo", "richard", "rick", "ricky", "rob", "robert", "roberto",
    "robin", "rodney", "roger", "ron", "ronald", "rose", "ross", "roy",
    "russell", "ruth", "ryan", "sabrina", "sally", "sam", "samantha", "samuel",
    "sandra", "sara", "sarah", "scott", "sean", "seth", "shannon", "sharon",
    "shawn", "sheila", "shelby", "sherry", "shirley", "sofia", "sonia",
    "sophia", "stacey", "stacy", "stanley", "stephanie", "stephen", "steve",
    "steven", "susan", "suzanne", "sydney", "tamara", "tammy", "tanya", "tara",
    "taylor", "ted", "teresa", "terri", "terry", "theresa", "thomas", "tim",
    "timothy", "tina", "todd", "tom", "tony", "tracy", "travis", "trevor",
    "troy", "tyler", "valerie", "vanessa", "vernon", "veronica", "vicki",
    "victor", "victoria", "vincent", "virginia", "walter", "wanda", "warren",
    "wayne", "wendy", "wesley", "william", "willie", "yvonne", "zachary", "zoe",
})


def guess_first_name(email: str | None) -> str | None:
    """Return capitalized first name guess, or None for generic / no-confidence cases."""
    if not email or "@" not in email:
        return None
    local = email.split("@", 1)[0].strip().lower()
    parts = re.split(r"[._+\-]", local)
    if not parts:
        return None
    first = parts[0]
    if not first or len(first) < 2:
        return None
    if first in GENERIC_LOCAL_PARTS:
        return None
    if any(ch.isdigit() for ch in first):
        return None
    if not re.fullmatch(r"[a-z]+", first):
        return None
    # Only a recognized first name is trusted, multi-segment or not: 'dr.steve67',
    # 'stone.smcstone' and 'domalakes_ins' put a title, a business word or a surname
    # first, and "Hi Dr," / "Hi Stone," is worse than "Hi there,".
    if first not in COMMON_FIRST_NAMES:
        return None
    return first.capitalize()


# Words a page or listing puts where a contact name goes ("Associations",
# "Insurance Policy") that are never someone's first name.
_NOT_NAMES = {"associations", "association", "associates", "insurance", "policy", "agency",
              "realty", "estate", "real", "office", "offices", "law", "legal", "dental",
              "clinic", "services", "company", "group", "partners", "members", "staff",
              "customer", "client", "clients", "patient", "patients", "contact", "owners",
              "about", "all", "our", "us", "welcome", "home", "team"}
_HONORIFICS = {"dr", "mr", "mrs", "ms", "miss", "mx", "the", "chef", "rev", "prof"}
# A plausible name token: letters (any script), optionally with ' or - inside.
_NAME_TOKEN = re.compile(r"^[^\W\d_]+(?:['\-][^\W\d_]+)*\.?$")


def looks_like_person_name(value: str | None) -> bool:
    """True only for short, letter-only strings that read like a person's name.

    Rejects the junk real spreadsheets contain in a "Contact Name" column: URLs
    (283 of 286 values in the master CSV are Restaurantji links), emails, phone
    numbers, role words, and long phrases.
    """
    if not value:
        return False
    v = value.strip()
    if not v or len(v) > 60:
        return False
    low = v.lower()
    if any(m in low for m in ("http", "www", ".com", "/", "@", "\\")):
        return False
    if any(ch.isdigit() for ch in v):
        return False
    tokens = v.replace(",", " ").split()
    if not 1 <= len(tokens) <= 4:
        return False
    if not all(_NAME_TOKEN.match(t) for t in tokens):
        return False
    real = [t for t in tokens if t.rstrip(".").lower() not in _HONORIFICS]
    if not real:
        return False
    if all(t.rstrip(".").lower() in GENERIC_LOCAL_PARTS for t in real):
        return False
    return True


def first_name_from_full(full_name: str | None) -> str | None:
    """Greeting name from a source-provided full name, or None.

    'Mary Smith' -> 'Mary'; 'José García' -> 'José'; 'Dr. James Schmitt' ->
    'Dr. Schmitt' (a doctor is greeted by title); 'Mrs Smith' -> None (no first
    name, and 'Hi Mrs' would be wrong). URLs and other junk -> None.
    """
    if not looks_like_person_name(full_name):
        return None
    tokens = full_name.strip().replace(",", " ").split()
    head = tokens[0].rstrip(".").lower()
    if head == "dr" and len(tokens) >= 2:
        return f"Dr. {tokens[-1].rstrip('.').capitalize()}"
    rest = [t for t in tokens if t.rstrip(".").lower() not in _HONORIFICS]
    if not rest:
        return None
    # "Mrs Smith": a title plus a single surname has no first name to greet with.
    if head in {"mr", "mrs", "ms", "miss", "mx"} and len(rest) == 1:
        return None
    first = rest[0].rstrip(".")
    if len(first) < 2 or first.lower() in GENERIC_LOCAL_PARTS or first.lower() in _NOT_NAMES:
        return None
    return first[:1].upper() + first[1:].lower()
