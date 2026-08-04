"""Placing a server from its name, and refusing to when the name says nothing."""
import json
import unittest
from unittest import mock

from procwatch import geoip


class TestAirportTags(unittest.TestCase):
    def test_the_big_networks_publish_their_city(self):
        cases = [
            ("lax28s17-in-f6.1e100.net", "Los Angeles"),
            ("edge-star-mini-shv-01-lhr8.facebook.com", "London"),
            ("server-18-65-1-2.fra56.r.cloudfront.net", "Frankfurt"),
            ("qro02s23-in-f10.1e100.net", "Queretaro"),
            ("lb-140-82-114-25-iad.github.com", "Ashburn"),
        ]
        for name, city in cases:
            found = geoip.locate("1.2.3.4", name)
            self.assertIsNotNone(found, name)
            self.assertEqual(found["city"], city, name)
            self.assertEqual(found["accuracy"], "city")

    def test_a_word_that_merely_contains_a_code_is_not_a_place(self):
        # "delta" holds "del", "session" holds "ses", "arnold" holds "arn".
        # Without a boundary rule every one of these lands somewhere.
        for name in ("delta-airlines.com", "sessions.example.com",
                     "arnold.example.com", "smfc.example.com"):
            found = geoip.locate("1.2.3.4", name)
            self.assertNotEqual((found or {}).get("accuracy"), "city", name)

    def test_the_domain_itself_is_not_read_as_a_tag(self):
        # apple.com, akamai.net -- the hint lives in the host part, and
        # reading the registered domain invents locations wholesale.
        found = geoip.locate("1.2.3.4", "www.apple.com")
        self.assertNotEqual((found or {}).get("accuracy"), "city")


class TestCountryFallback(unittest.TestCase):
    def test_a_country_domain_places_a_country(self):
        found = geoip.locate("1.2.3.4", "host.example.jp")
        self.assertEqual(found["country"], "JP")
        self.assertEqual(found["accuracy"], "country")

    def test_second_level_country_domains(self):
        found = geoip.locate("1.2.3.4", "www.example.co.uk")
        self.assertEqual(found["country"], "GB")

    def test_a_plain_com_is_nowhere(self):
        self.assertIsNone(geoip.locate("1.2.3.4", "example.com"))

    def test_an_address_with_no_name_is_nowhere(self):
        self.assertIsNone(geoip.locate("93.184.216.34", ""))

    def test_a_name_arriving_in_the_host_field_still_counts(self):
        # nettop returns a name rather than an address for peers it has
        # already resolved; the hint is in the host field either way.
        found = geoip.locate("lax28s17-in-f6.1e100.net", "")
        self.assertEqual(found["city"], "Los Angeles")


class TestPrivate(unittest.TestCase):
    def test_the_local_network_is_recognised(self):
        for host in ("127.0.0.1", "10.0.0.5", "192.168.1.20",
                     "172.16.4.4", "169.254.1.1", "::1", "fe80::1"):
            found = geoip.locate(host, "")
            self.assertEqual(found["accuracy"], "local", host)
            self.assertIsNone(found["lat"])

    def test_a_public_address_is_not_local(self):
        for host in ("8.8.8.8", "17.253.83.150", "172.15.1.1", "172.32.1.1"):
            self.assertFalse(geoip.is_private(host), host)


class TestLookupService(unittest.TestCase):
    """The service half: cached, never blocking, and never told about the
    machines on your own network."""

    def setUp(self):
        geoip._cache.clear()
        del geoip._queue[:]
        self.addCleanup(geoip._cache.clear)
        self.addCleanup(lambda: geoip._queue.clear())

    def test_an_unknown_address_is_queued_not_waited_for(self):
        with mock.patch.object(geoip, "_load_cache"), \
                mock.patch.object(geoip, "_start") as start:
            found = geoip.where("93.184.216.34", "", allow_lookup=True)
        self.assertIn(("ip", "93.184.216.34"), geoip._queue)
        self.assertTrue(start.called)
        # Nothing is known yet, so it falls back to what the name says --
        # here, nothing at all.
        self.assertIsNone(found)

    def test_a_cached_answer_is_used(self):
        geoip._cache["93.184.216.34"] = (51.5, -0.12, "London", "England",
                                         "GB", "Example Ltd", 1)
        with mock.patch.object(geoip, "_load_cache"):
            found = geoip.where("93.184.216.34", "")
        self.assertEqual(found["city"], "London")
        self.assertEqual(found["accuracy"], "lookup")
        self.assertEqual(geoip._queue, [])

    def test_an_address_the_service_could_not_place_is_not_asked_twice(self):
        geoip._cache["93.184.216.34"] = (None, None, "", "", "", "", 0)
        with mock.patch.object(geoip, "_load_cache"):
            geoip.where("93.184.216.34", "")
        self.assertEqual(geoip._queue, [])

    def test_a_name_that_does_not_resolve_is_not_asked_about_again(self):
        geoip._addr["nowhere.example"] = ""
        self.addCleanup(geoip._addr.clear)
        with mock.patch.object(geoip, "_load_cache"), \
                mock.patch.object(geoip, "_start"):
            geoip.where("nowhere.example", "")
        self.assertEqual(geoip._queue, [])

    def test_private_addresses_are_never_sent(self):
        for host in ("10.0.0.5", "192.168.1.9", "127.0.0.1"):
            found = geoip.where(host, "", allow_lookup=True)
            self.assertEqual(found["accuracy"], "local", host)
        self.assertEqual(geoip._queue, [])

    def test_with_lookups_off_nothing_is_queued(self):
        found = geoip.where("93.184.216.34", "lax28s17-in-f6.1e100.net",
                            allow_lookup=False)
        self.assertEqual(geoip._queue, [])
        # and the name still places it
        self.assertEqual(found["city"], "Los Angeles")

    def test_a_named_peer_is_resolved_before_it_is_looked_up(self):
        # nettop hands back a name for peers it has already resolved. A name
        # cannot be looked up, so it becomes an address first -- and until
        # that lands, the name's own hint still answers.
        with mock.patch.object(geoip, "_load_cache"), \
                mock.patch.object(geoip, "_start"):
            found = geoip.where("lax28s17-in-f6.1e100.net", "")
        self.assertIn(("name", "lax28s17-in-f6.1e100.net"), geoip._queue)
        self.assertEqual(found["city"], "Los Angeles")

    def test_a_resolved_name_uses_the_address_answer(self):
        geoip._addr["cdn.example.com"] = "93.184.216.34"
        geoip._cache["93.184.216.34"] = (35.7, 139.7, "Tokyo", "", "JP", "", 1)
        self.addCleanup(geoip._addr.clear)
        with mock.patch.object(geoip, "_load_cache"):
            found = geoip.where("cdn.example.com", "")
        self.assertEqual(found["city"], "Tokyo")

    def test_the_service_answer_is_parsed(self):
        payload = json.dumps({
            "success": True, "ip": "8.8.8.8", "city": "Mountain View",
            "region": "California", "country_code": "US",
            "latitude": 37.4, "longitude": -122.07,
            "connection": {"org": "Google LLC"}}).encode()
        with mock.patch.object(geoip.urllib.request, "urlopen",
                               return_value=_Reply(payload)):
            found = geoip.ask("8.8.8.8")
        self.assertEqual(found["city"], "Mountain View")
        self.assertEqual(found["country"], "US")
        self.assertEqual(found["org"], "Google LLC")

    def test_a_refusal_is_not_a_place(self):
        payload = json.dumps({"success": False, "message": "quota"}).encode()
        with mock.patch.object(geoip.urllib.request, "urlopen",
                               return_value=_Reply(payload)):
            self.assertIsNone(geoip.ask("8.8.8.8"))


class _Reply(object):
    def __init__(self, payload):
        self.payload = payload

    def read(self, *args):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestTable(unittest.TestCase):
    def test_every_city_carries_a_plausible_coordinate(self):
        for tag, (lat, lon, name, country) in geoip.CITIES.items():
            self.assertEqual(len(tag), 3, tag)
            self.assertTrue(-90 <= lat <= 90, tag)
            self.assertTrue(-180 <= lon <= 180, tag)
            self.assertTrue(name and len(country) == 2, tag)


if __name__ == "__main__":
    unittest.main()
