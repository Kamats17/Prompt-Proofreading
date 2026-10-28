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
