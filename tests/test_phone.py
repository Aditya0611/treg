from app.normalize import clean_phone


def test_european_number_keeps_its_country_code():
    assert clean_phone("+45 87 38 90 11") == "+4587389011"
    assert clean_phone("+32 5 643 05 11") == "+325643051" + "1"


def test_us_numbers_keep_the_us_shape():
    assert clean_phone("+1 818-709-8505") == "+1 818-709-8505"
    assert clean_phone("(818) 709-8505") == "+1 818-709-8505"


def test_indian_number_keeps_its_shape():
    assert clean_phone("+91 98765 43210") == "+91 98765 43210"
