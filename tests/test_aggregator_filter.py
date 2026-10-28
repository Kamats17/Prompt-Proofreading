from app.safety.aggregator_filter import is_aggregator


def test_blocks_yelp():
    agg, reason = is_aggregator("https://yelp.com/biz/bob-diner")
    assert agg
    assert "yelp" in reason


def test_blocks_facebook():
    agg, _ = is_aggregator("https://www.facebook.com/bobsdiner")
    assert agg


def test_blocks_restaurantji():
    agg, _ = is_aggregator("https://restaurantji.com/al/thorsby/farm-house-caf")
    assert agg


def test_blocks_placeid():
    agg, _ = is_aggregator("https://fatheadz-barber-shop.placeid.site")
    assert agg


def test_blocks_wixsite_subdomain():
    agg, reason = is_aggregator("https://mysalon.wixsite.com/home")
    assert agg
    assert "wixsite.com" in reason


def test_blocks_myshopify():
    agg, _ = is_aggregator("https://acmewidgets.myshopify.com")
    assert agg


def test_real_business_passes():
    agg, _ = is_aggregator("https://bobsdiner.com")
    assert not agg
    agg, _ = is_aggregator("https://promptproofreading.com")
    assert not agg


def test_empty_passes():
    agg, _ = is_aggregator("")
    assert not agg
