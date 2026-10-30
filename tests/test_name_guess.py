from app.draft.name_guess import guess_first_name, first_name_from_full


def test_personal_email():
    assert guess_first_name("mary@bobsdiner.com") == "Mary"
    assert guess_first_name("Mary.Smith@example.com") == "Mary"
    assert guess_first_name("john_doe@x.com") == "John"
    assert guess_first_name("sarah-jane@x.com") == "Sarah"


def test_single_token_nonname_returns_none():
    # Last-name login or company-derived local must NOT become a fake greeting.
    assert guess_first_name("ckamats@promptproofreading.com") is None
    assert guess_first_name("bobsdiner@gmail.com") is None


def test_first_name_from_full():
    assert first_name_from_full("Mary Smith") == "Mary"
    assert first_name_from_full("  john   doe ") == "John"
    assert first_name_from_full("Owner") is None
    assert first_name_from_full("") is None
    assert first_name_from_full(None) is None


def test_generic_locals_return_none():
    for local in ["info", "contact", "hello", "sales", "owner", "manager",
                  "admin", "support", "team", "noreply", "billing"]:
        assert guess_first_name(f"{local}@x.com") is None, local


def test_numbers_or_short_locals_return_none():
    assert guess_first_name("a@x.com") is None
    assert guess_first_name("user123@x.com") is None
    assert guess_first_name("4me@x.com") is None


def test_invalid_inputs():
    assert guess_first_name(None) is None
    assert guess_first_name("") is None
    assert guess_first_name("no-at-sign") is None


def test_titles_business_words_and_surnames_are_not_greetings():
    from app.draft.pipeline import resolve_greeting_for
    assert guess_first_name("dr.steve67@gmail.com") is None
    assert guess_first_name("stone.smcstone@gmail.com") is None
    assert guess_first_name("domalakes_ins@hotmail.com") is None
    assert first_name_from_full("Associations") is None
    assert first_name_from_full("Insurance Policy") is None
    assert resolve_greeting_for("SMC Stone", explicit="Stone", contact_name=None,
                                owner_first_name=None, email=None) != "Stone"
    assert resolve_greeting_for("Larry Domalakes Insurance", explicit=None, contact_name="Larry",
                                owner_first_name=None, email=None) == "Larry"
    assert resolve_greeting_for("The Law Office of Nicholas Forrest Colvin", explicit="Nicholas",
                                contact_name=None, owner_first_name=None, email=None) == "Nicholas"
    assert first_name_from_full("About Us") is None
