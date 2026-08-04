"""Where a server is, and where this Mac is.

Two ways of answering, in that order.

The good one is a lookup service. ipwho.is answers over HTTPS without a key,
knows the city and the network operator, and -- asked about nothing in
particular -- reports this machine's own public address, which is the only
way a program with no location permission can honestly say where "here" is.
Answers are cached in the database, so an address is asked about once.

That means peer addresses leave this Mac, which is a real cost for a tool
whose whole promise is that nothing does. So it is a setting, the setting is
visible, private addresses are never sent, and turning it off leaves the
second way working.

The second way is the names themselves, and they give away a surprising
amount: the large networks put a routing hint in every reverse-DNS name they
publish.

    lax28s17-in-f6.1e100.net          Google, Los Angeles
    edge-star-mini-shv-01-lhr8        Meta, London
    server-18-65-x-x.fra56.r          CloudFront, Frankfurt
    ae-1.a01.tokyjp05.jp.bb           NTT, Tokyo

Those three-letter tags are IATA airport codes, and an airport is a city.
The table below is the busiest interchange cities -- where the machines
answering a Mac in practice actually are.

Everything here is an estimate and the interface says so. A wrong city on a
map is a small lie; a map that pretends to be precise is a larger one.
"""
import json
import re
import threading
import time
import urllib.request

# The world's traffic does not spread evenly, so neither does this: these are
# the cities that terminate consumer connections. (lat, lon, name, country)
CITIES = {
    # North America
    "iad": (38.94, -77.46, "Ashburn", "US"),
    "dca": (38.85, -77.04, "Washington", "US"),
    "bwi": (39.18, -76.67, "Baltimore", "US"),
    "ewr": (40.69, -74.17, "Newark", "US"),
    "jfk": (40.64, -73.78, "New York", "US"),
    "lga": (40.78, -73.87, "New York", "US"),
    "nyc": (40.71, -74.01, "New York", "US"),
    "bos": (42.36, -71.01, "Boston", "US"),
    "phl": (39.87, -75.24, "Philadelphia", "US"),
    "atl": (33.64, -84.43, "Atlanta", "US"),
    "mia": (25.79, -80.29, "Miami", "US"),
    "mco": (28.43, -81.31, "Orlando", "US"),
    "tpa": (27.98, -82.53, "Tampa", "US"),
    "clt": (35.21, -80.94, "Charlotte", "US"),
    "iah": (29.98, -95.34, "Houston", "US"),
    "dfw": (32.90, -97.04, "Dallas", "US"),
    "aus": (30.19, -97.67, "Austin", "US"),
    "den": (39.86, -104.67, "Denver", "US"),
    "ord": (41.98, -87.90, "Chicago", "US"),
    "chi": (41.88, -87.63, "Chicago", "US"),
    "mci": (39.30, -94.71, "Kansas City", "US"),
    "msp": (44.88, -93.22, "Minneapolis", "US"),
    "det": (42.33, -83.05, "Detroit", "US"),
    "cmh": (39.998, -82.89, "Columbus", "US"),
    "slc": (40.79, -111.98, "Salt Lake City", "US"),
    "phx": (33.44, -112.01, "Phoenix", "US"),
    "las": (36.08, -115.15, "Las Vegas", "US"),
    "lax": (33.94, -118.41, "Los Angeles", "US"),
    "sjc": (37.36, -121.93, "San Jose", "US"),
    "sfo": (37.62, -122.38, "San Francisco", "US"),
    "pdx": (45.59, -122.60, "Portland", "US"),
    "sea": (47.45, -122.31, "Seattle", "US"),
    "yyz": (43.68, -79.63, "Toronto", "CA"),
    "yul": (45.47, -73.74, "Montreal", "CA"),
    "yvr": (49.19, -123.18, "Vancouver", "CA"),
    "mex": (19.44, -99.07, "Mexico City", "MX"),
    # South America
    "gru": (-23.43, -46.47, "Sao Paulo", "BR"),
    "gig": (-22.81, -43.25, "Rio de Janeiro", "BR"),
    "eze": (-34.82, -58.54, "Buenos Aires", "AR"),
    "scl": (-33.39, -70.79, "Santiago", "CL"),
    "bog": (4.70, -74.15, "Bogota", "CO"),
    "lim": (-12.02, -77.11, "Lima", "PE"),
    # Europe
    "lhr": (51.47, -0.45, "London", "GB"),
    "lon": (51.51, -0.13, "London", "GB"),
    "lcy": (51.51, 0.05, "London", "GB"),
    "man": (53.35, -2.27, "Manchester", "GB"),
    "dub": (53.43, -6.25, "Dublin", "IE"),
    "ams": (52.31, 4.76, "Amsterdam", "NL"),
    "bru": (50.90, 4.48, "Brussels", "BE"),
    "cdg": (49.01, 2.55, "Paris", "FR"),
    "par": (48.86, 2.35, "Paris", "FR"),
    "ory": (48.72, 2.38, "Paris", "FR"),
    "mrs": (43.44, 5.22, "Marseille", "FR"),
    "fra": (50.04, 8.56, "Frankfurt", "DE"),
    "ber": (52.52, 13.40, "Berlin", "DE"),
    "muc": (48.35, 11.79, "Munich", "DE"),
    "ham": (53.63, 9.99, "Hamburg", "DE"),
    "dus": (51.29, 6.77, "Dusseldorf", "DE"),
    "zrh": (47.46, 8.55, "Zurich", "CH"),
    "gva": (46.24, 6.11, "Geneva", "CH"),
    "vie": (48.11, 16.57, "Vienna", "AT"),
    "prg": (50.10, 14.26, "Prague", "CZ"),
    "waw": (52.17, 20.97, "Warsaw", "PL"),
    "cph": (55.62, 12.66, "Copenhagen", "DK"),
    "aal": (57.09, 9.85, "Aalborg", "DK"),
    "arn": (59.65, 17.92, "Stockholm", "SE"),
    "osl": (60.19, 11.10, "Oslo", "NO"),
    "hel": (60.32, 24.96, "Helsinki", "FI"),
    "mad": (40.47, -3.56, "Madrid", "ES"),
    "bcn": (41.30, 2.08, "Barcelona", "ES"),
    "lis": (38.77, -9.13, "Lisbon", "PT"),
    "mil": (45.46, 9.19, "Milan", "IT"),
    "mxp": (45.63, 8.72, "Milan", "IT"),
    "fco": (41.80, 12.25, "Rome", "IT"),
    "ath": (37.94, 23.95, "Athens", "GR"),
    "ist": (41.28, 28.75, "Istanbul", "TR"),
    "otp": (44.57, 26.10, "Bucharest", "RO"),
    "sof": (42.70, 23.41, "Sofia", "BG"),
    "bud": (47.44, 19.26, "Budapest", "HU"),
    "svo": (55.97, 37.41, "Moscow", "RU"),
    "dme": (55.41, 37.90, "Moscow", "RU"),
    "led": (59.80, 30.26, "St Petersburg", "RU"),
    "kbp": (50.34, 30.89, "Kyiv", "UA"),
    "kef": (63.99, -22.62, "Reykjavik", "IS"),
    # Middle East and Africa
    "dxb": (25.25, 55.36, "Dubai", "AE"),
    "auh": (24.43, 54.65, "Abu Dhabi", "AE"),
    "doh": (25.27, 51.61, "Doha", "QA"),
    "tlv": (32.01, 34.89, "Tel Aviv", "IL"),
    "ruh": (24.96, 46.70, "Riyadh", "SA"),
    "cai": (30.11, 31.41, "Cairo", "EG"),
    "jnb": (-26.13, 28.24, "Johannesburg", "ZA"),
    "cpt": (-33.97, 18.60, "Cape Town", "ZA"),
    "los": (6.58, 3.32, "Lagos", "NG"),
    "nbo": (-1.32, 36.93, "Nairobi", "KE"),
    # Asia and Oceania
    "bom": (19.09, 72.87, "Mumbai", "IN"),
    "del": (28.56, 77.10, "Delhi", "IN"),
    "maa": (12.99, 80.17, "Chennai", "IN"),
    "blr": (13.20, 77.71, "Bengaluru", "IN"),
    "hyd": (17.24, 78.43, "Hyderabad", "IN"),
    "cmb": (7.18, 79.88, "Colombo", "LK"),
    "sin": (1.36, 103.99, "Singapore", "SG"),
    "kul": (2.75, 101.71, "Kuala Lumpur", "MY"),
    "bkk": (13.69, 100.75, "Bangkok", "TH"),
    "cgk": (-6.13, 106.66, "Jakarta", "ID"),
    "mnl": (14.51, 121.02, "Manila", "PH"),
    "hkg": (22.31, 113.91, "Hong Kong", "HK"),
    "tpe": (25.08, 121.23, "Taipei", "TW"),
    "pvg": (31.14, 121.81, "Shanghai", "CN"),
    "sha": (31.23, 121.47, "Shanghai", "CN"),
    "pek": (40.08, 116.58, "Beijing", "CN"),
    "can": (23.39, 113.30, "Guangzhou", "CN"),
    "icn": (37.46, 126.44, "Seoul", "KR"),
    "sel": (37.57, 126.98, "Seoul", "KR"),
    "nrt": (35.77, 140.39, "Tokyo", "JP"),
    "hnd": (35.55, 139.78, "Tokyo", "JP"),
    "tyo": (35.68, 139.69, "Tokyo", "JP"),
    "kix": (34.43, 135.23, "Osaka", "JP"),
    "osa": (34.69, 135.50, "Osaka", "JP"),
    "syd": (-33.94, 151.18, "Sydney", "AU"),
    "mel": (-37.67, 144.84, "Melbourne", "AU"),
    "bne": (-27.38, 153.12, "Brisbane", "AU"),
    "per": (-31.94, 115.97, "Perth", "AU"),
    "akl": (-37.01, 174.79, "Auckland", "NZ"),
    "hnl": (21.32, -157.92, "Honolulu", "US"),
    # The rest of the interchange map. Long, because a table that stops at
    # the famous airports sends most of a real machine's traffic to nowhere:
    # the large content networks answer from second-tier cities on purpose,
    # and the whole point of reading these tags is that they are specific.
    "buf": (42.94, -78.73, "Buffalo", "US"),
    "cle": (41.41, -81.85, "Cleveland", "US"),
    "cid": (41.88, -91.71, "Cedar Rapids", "US"),
    "cbf": (41.26, -95.86, "Council Bluffs", "US"),
    "dls": (45.62, -121.17, "The Dalles", "US"),
    "dal": (32.85, -96.85, "Dallas", "US"),
    "dtw": (42.21, -83.35, "Detroit", "US"),
    "fll": (26.07, -80.15, "Fort Lauderdale", "US"),
    "hou": (29.65, -95.28, "Houston", "US"),
    "ind": (39.72, -86.29, "Indianapolis", "US"),
    "jax": (30.49, -81.69, "Jacksonville", "US"),
    "mrn": (35.82, -81.61, "Lenoir", "US"),
    "nuq": (37.42, -122.05, "Mountain View", "US"),
    "pao": (37.46, -122.11, "Palo Alto", "US"),
    "pit": (40.49, -80.23, "Pittsburgh", "US"),
    "pwk": (42.11, -87.90, "Chicago", "US"),
    "rdu": (35.88, -78.79, "Raleigh", "US"),
    "ric": (37.51, -77.32, "Richmond", "US"),
    "sat": (29.53, -98.47, "San Antonio", "US"),
    "smf": (38.70, -121.59, "Sacramento", "US"),
    "san": (32.73, -117.19, "San Diego", "US"),
    "stl": (38.75, -90.37, "St Louis", "US"),
    "abq": (35.04, -106.61, "Albuquerque", "US"),
    "oma": (41.30, -95.89, "Omaha", "US"),
    "okc": (35.39, -97.60, "Oklahoma City", "US"),
    "bna": (36.13, -86.68, "Nashville", "US"),
    "msy": (29.99, -90.26, "New Orleans", "US"),
    "anc": (61.17, -149.99, "Anchorage", "US"),
    "yyc": (51.13, -114.01, "Calgary", "CA"),
    "yow": (45.32, -75.67, "Ottawa", "CA"),
    "yhu": (45.52, -73.42, "Montreal", "CA"),
    "yxu": (43.03, -81.15, "London", "CA"),
    "ywg": (49.91, -97.24, "Winnipeg", "CA"),
    "qro": (20.62, -100.19, "Queretaro", "MX"),
    "gdl": (20.52, -103.31, "Guadalajara", "MX"),
    "mty": (25.78, -100.11, "Monterrey", "MX"),
    "pty": (9.07, -79.38, "Panama City", "PA"),
    "sju": (18.44, -66.00, "San Juan", "PR"),
    "cgh": (-23.63, -46.66, "Sao Paulo", "BR"),
    "vcp": (-23.01, -47.13, "Campinas", "BR"),
    "poa": (-29.99, -51.17, "Porto Alegre", "BR"),
    "cnf": (-19.62, -43.97, "Belo Horizonte", "BR"),
    "for": (-3.78, -38.53, "Fortaleza", "BR"),
    "uio": (-0.13, -78.36, "Quito", "EC"),
    "mvd": (-34.84, -56.03, "Montevideo", "UY"),
    "asu": (-25.24, -57.52, "Asuncion", "PY"),
    "cor": (-31.32, -64.21, "Cordoba", "AR"),
    "lpp": (60.95, 28.15, "Hamina", "FI"),
    "tll": (59.41, 24.83, "Tallinn", "EE"),
    "rix": (56.92, 23.97, "Riga", "LV"),
    "vno": (54.64, 25.28, "Vilnius", "LT"),
    "str": (48.69, 9.22, "Stuttgart", "DE"),
    "cgn": (50.87, 7.14, "Cologne", "DE"),
    "lej": (51.42, 12.24, "Leipzig", "DE"),
    "nue": (49.50, 11.08, "Nuremberg", "DE"),
    "brm": (53.05, 8.79, "Bremen", "DE"),
    "hhn": (49.95, 7.26, "Frankfurt", "DE"),
    "ein": (51.45, 5.37, "Eindhoven", "NL"),
    "rtm": (51.96, 4.44, "Rotterdam", "NL"),
    "got": (57.66, 12.28, "Gothenburg", "SE"),
    "mma": (55.53, 13.37, "Malmo", "SE"),
    "bgo": (60.29, 5.22, "Bergen", "NO"),
    "trd": (63.46, 10.92, "Trondheim", "NO"),
    "aar": (56.30, 10.62, "Aarhus", "DK"),
    "bll": (55.74, 9.15, "Billund", "DK"),
    "gla": (55.87, -4.43, "Glasgow", "GB"),
    "edi": (55.95, -3.37, "Edinburgh", "GB"),
    "brs": (51.38, -2.72, "Bristol", "GB"),
    "lpl": (53.34, -2.85, "Liverpool", "GB"),
    "ork": (51.84, -8.49, "Cork", "IE"),
    "lyn": (45.73, 5.08, "Lyon", "FR"),
    "lys": (45.73, 5.08, "Lyon", "FR"),
    "bod": (44.83, -0.72, "Bordeaux", "FR"),
    "tls": (43.63, 1.36, "Toulouse", "FR"),
    "nce": (43.66, 7.22, "Nice", "FR"),
    "lil": (50.56, 3.09, "Lille", "FR"),
    "rns": (48.07, -1.73, "Rennes", "FR"),
    "vlc": (39.49, -0.48, "Valencia", "ES"),
    "svq": (37.42, -5.90, "Seville", "ES"),
    "agp": (36.67, -4.50, "Malaga", "ES"),
    "bio": (43.30, -2.91, "Bilbao", "ES"),
    "opo": (41.25, -8.68, "Porto", "PT"),
    "nap": (40.89, 14.29, "Naples", "IT"),
    "trn": (45.20, 7.65, "Turin", "IT"),
    "vce": (45.51, 12.35, "Venice", "IT"),
    "blq": (44.53, 11.30, "Bologna", "IT"),
    "plm": (39.55, 2.74, "Palma", "ES"),
    "krk": (50.08, 19.79, "Krakow", "PL"),
    "gdn": (54.38, 18.47, "Gdansk", "PL"),
    "poz": (52.42, 16.83, "Poznan", "PL"),
    "brq": (49.15, 16.69, "Brno", "CZ"),
    "bts": (48.17, 17.21, "Bratislava", "SK"),
    "zag": (45.74, 16.07, "Zagreb", "HR"),
    "beg": (44.82, 20.31, "Belgrade", "RS"),
    "skp": (41.96, 21.62, "Skopje", "MK"),
    "tia": (41.41, 19.72, "Tirana", "AL"),
    "lca": (34.88, 33.63, "Larnaca", "CY"),
    "vlt": (35.86, 14.48, "Valletta", "MT"),
    "ayt": (36.90, 30.79, "Antalya", "TR"),
    "esb": (40.13, 32.99, "Ankara", "TR"),
    "adb": (38.29, 27.16, "Izmir", "TR"),
    "svx": (56.75, 60.80, "Yekaterinburg", "RU"),
    "ovb": (55.01, 82.65, "Novosibirsk", "RU"),
    "kzn": (55.61, 49.28, "Kazan", "RU"),
    "amd": (23.07, 72.63, "Ahmedabad", "IN"),
    "pnq": (18.58, 73.92, "Pune", "IN"),
    "ccu": (22.65, 88.45, "Kolkata", "IN"),
    "cok": (10.15, 76.40, "Kochi", "IN"),
    "dac": (23.84, 90.40, "Dhaka", "BD"),
    "khi": (24.91, 67.16, "Karachi", "PK"),
    "isb": (33.55, 72.83, "Islamabad", "PK"),
    "khh": (22.58, 120.35, "Kaohsiung", "TW"),
    "sgn": (10.82, 106.66, "Ho Chi Minh City", "VN"),
    "han": (21.22, 105.81, "Hanoi", "VN"),
    "pnh": (11.55, 104.84, "Phnom Penh", "KH"),
    "rgn": (16.91, 96.13, "Yangon", "MM"),
    "dps": (-8.75, 115.17, "Denpasar", "ID"),
    "sub": (-7.38, 112.79, "Surabaya", "ID"),
    "jhb": (1.64, 103.67, "Johor Bahru", "MY"),
    "ceb": (10.31, 123.98, "Cebu", "PH"),
    "szx": (22.64, 113.81, "Shenzhen", "CN"),
    "ctu": (30.58, 103.95, "Chengdu", "CN"),
    "hgh": (30.23, 120.43, "Hangzhou", "CN"),
    "tsn": (39.12, 117.35, "Tianjin", "CN"),
    "ngo": (34.86, 136.81, "Nagoya", "JP"),
    "fuk": (33.59, 130.45, "Fukuoka", "JP"),
    "cts": (42.78, 141.69, "Sapporo", "JP"),
    "pus": (35.18, 128.94, "Busan", "KR"),
    "adl": (-34.95, 138.53, "Adelaide", "AU"),
    "cbr": (-35.31, 149.20, "Canberra", "AU"),
    "hba": (-42.84, 147.51, "Hobart", "AU"),
    "wlg": (-41.33, 174.81, "Wellington", "NZ"),
    "chc": (-43.49, 172.53, "Christchurch", "NZ"),
    "dur": (-29.61, 31.12, "Durban", "ZA"),
    "acc": (5.61, -0.17, "Accra", "GH"),
    "abj": (5.26, -3.93, "Abidjan", "CI"),
    "dkr": (14.74, -17.49, "Dakar", "SN"),
    "add": (8.98, 38.80, "Addis Ababa", "ET"),
    "dar": (-6.88, 39.20, "Dar es Salaam", "TZ"),
    "cmn": (33.37, -7.59, "Casablanca", "MA"),
    "tun": (36.85, 10.23, "Tunis", "TN"),
    "alg": (36.69, 3.22, "Algiers", "DZ"),
    "jed": (21.68, 39.16, "Jeddah", "SA"),
    "kwi": (29.23, 47.98, "Kuwait City", "KW"),
    "bah": (26.27, 50.63, "Manama", "BH"),
    "mct": (23.59, 58.28, "Muscat", "OM"),
    "amm": (31.72, 35.99, "Amman", "JO"),
    "bey": (33.82, 35.49, "Beirut", "LB"),
    "thr": (35.69, 51.39, "Tehran", "IR"),
    "tas": (41.26, 69.28, "Tashkent", "UZ"),
    "ala": (43.35, 77.04, "Almaty", "KZ"),
    "gyd": (40.47, 50.05, "Baku", "AZ"),
    "tbs": (41.67, 44.95, "Tbilisi", "GE"),
    "evn": (40.15, 44.40, "Yerevan", "AM"),
    "gua": (14.58, -90.53, "Guatemala City", "GT"),
    "sjo": (9.99, -84.21, "San Jose", "CR"),
    "hav": (23.13, -82.41, "Havana", "CU"),
    "sdq": (18.43, -69.67, "Santo Domingo", "DO"),
    "kin": (17.94, -76.79, "Kingston", "JM"),
}

# Where a country-code domain suggests, when no airport tag is present.
# Coarser on purpose: this places a server in a country, not a city.
COUNTRIES = {
    "uk": (51.51, -0.13, "United Kingdom", "GB"),
    "gb": (51.51, -0.13, "United Kingdom", "GB"),
    "ie": (53.35, -6.26, "Ireland", "IE"),
    "de": (51.16, 10.45, "Germany", "DE"),
    "fr": (46.60, 2.21, "France", "FR"),
    "nl": (52.13, 5.29, "Netherlands", "NL"),
    "be": (50.50, 4.47, "Belgium", "BE"),
    "ch": (46.82, 8.23, "Switzerland", "CH"),
    "at": (47.52, 14.55, "Austria", "AT"),
    "it": (41.87, 12.57, "Italy", "IT"),
    "es": (40.46, -3.75, "Spain", "ES"),
    "pt": (39.40, -8.22, "Portugal", "PT"),
    "se": (60.13, 18.64, "Sweden", "SE"),
    "no": (60.47, 8.47, "Norway", "NO"),
    "dk": (56.26, 9.50, "Denmark", "DK"),
    "fi": (61.92, 25.75, "Finland", "FI"),
    "pl": (51.92, 19.15, "Poland", "PL"),
    "cz": (49.82, 15.47, "Czechia", "CZ"),
    "ru": (55.75, 37.62, "Russia", "RU"),
    "ua": (48.38, 31.17, "Ukraine", "UA"),
    "tr": (38.96, 35.24, "Turkey", "TR"),
    "gr": (39.07, 21.82, "Greece", "GR"),
    "il": (31.05, 34.85, "Israel", "IL"),
    "ae": (23.42, 53.85, "United Arab Emirates", "AE"),
    "in": (20.59, 78.96, "India", "IN"),
    "cn": (35.86, 104.20, "China", "CN"),
    "jp": (36.20, 138.25, "Japan", "JP"),
    "kr": (35.91, 127.77, "South Korea", "KR"),
    "sg": (1.35, 103.82, "Singapore", "SG"),
    "hk": (22.32, 114.17, "Hong Kong", "HK"),
    "tw": (23.70, 120.96, "Taiwan", "TW"),
    "au": (-25.27, 133.78, "Australia", "AU"),
    "nz": (-40.90, 174.89, "New Zealand", "NZ"),
    "br": (-14.24, -51.93, "Brazil", "BR"),
    "ar": (-38.42, -63.62, "Argentina", "AR"),
    "cl": (-35.68, -71.54, "Chile", "CL"),
    "mx": (23.63, -102.55, "Mexico", "MX"),
    "ca": (56.13, -106.35, "Canada", "CA"),
    "za": (-30.56, 22.94, "South Africa", "ZA"),
    "eg": (26.82, 30.80, "Egypt", "EG"),
    "ng": (9.08, 8.68, "Nigeria", "NG"),
    "ke": (-0.02, 37.91, "Kenya", "KE"),
    "th": (15.87, 100.99, "Thailand", "TH"),
    "id": (-0.79, 113.92, "Indonesia", "ID"),
    "my": (4.21, 101.98, "Malaysia", "MY"),
    "ph": (12.88, 121.77, "Philippines", "PH"),
    "vn": (14.06, 108.28, "Vietnam", "VN"),
}

# A tag is only read where a hostname would carry one: inside a dotted label,
# bounded by something that is not a letter. Without the boundaries "delta"
# contains "del" and every server lands in Delhi.
_TAG = re.compile(r"(?:^|[^a-z])([a-z]{3})(?:[^a-z]|$)")

# Some networks write the city out instead of coding it -- edge-london-3,
# fra-de-01, ams1.example.net. Long enough to be unambiguous, so these are
# matched as whole words anywhere in the name.
SPELLED = {
    "london": "lhr", "manchester": "man", "dublin": "dub",
    "amsterdam": "ams", "frankfurt": "fra", "berlin": "ber",
    "munich": "muc", "hamburg": "ham", "paris": "cdg", "marseille": "mrs",
    "madrid": "mad", "barcelona": "bcn", "lisbon": "lis", "milan": "mil",
    "rome": "fco", "zurich": "zrh", "geneva": "gva", "vienna": "vie",
    "prague": "prg", "warsaw": "waw", "stockholm": "arn", "oslo": "osl",
    "copenhagen": "cph", "helsinki": "hel", "athens": "ath",
    "istanbul": "ist", "moscow": "svo", "brussels": "bru",
    "newyork": "nyc", "chicago": "chi", "dallas": "dfw", "denver": "den",
    "atlanta": "atl", "miami": "mia", "seattle": "sea", "portland": "pdx",
    "boston": "bos", "phoenix": "phx", "houston": "iah", "ashburn": "iad",
    "virginia": "iad", "oregon": "pdx", "california": "sjc",
    "losangeles": "lax", "sanjose": "sjc", "sanfrancisco": "sfo",
    "toronto": "yyz", "montreal": "yul", "vancouver": "yvr",
    "saopaulo": "gru", "santiago": "scl", "bogota": "bog", "lima": "lim",
    "buenosaires": "eze", "mexico": "mex",
    "singapore": "sin", "tokyo": "tyo", "osaka": "osa", "seoul": "icn",
    "mumbai": "bom", "delhi": "del", "chennai": "maa",
    "bangalore": "blr", "bengaluru": "blr", "hongkong": "hkg",
    "taipei": "tpe", "shanghai": "sha", "beijing": "pek",
    "jakarta": "cgk", "bangkok": "bkk", "manila": "mnl",
    "sydney": "syd", "melbourne": "mel", "brisbane": "bne", "perth": "per",
    "auckland": "akl", "dubai": "dxb", "telaviv": "tlv",
    "johannesburg": "jnb", "capetown": "cpt", "nairobi": "nbo",
    "lagos": "los", "cairo": "cai",
}
_WORD = re.compile(r"[a-z]{4,}")

_PRIVATE = re.compile(r"^(?:10\.|127\.|192\.168\.|169\.254\.|"
                      r"172\.(?:1[6-9]|2\d|3[01])\.|::1$|fe80:|f[cd])")


def is_private(host):
    """Whether this address is on the machine or the local network."""
    return bool(host and _PRIVATE.match(host.lower()))


def locate(host, name=""):
    """Where a peer probably is: {lat, lon, city, country, accuracy}, or None.

    `name` is the reverse-DNS name when one is known -- it is what carries
    the hint, so an unnamed address usually places nowhere. Accuracy says
    which rule fired, so the interface can be honest about the guess.
    """
    if is_private(host):
        return {"lat": None, "lon": None, "city": "This network",
                "country": "", "accuracy": "local"}
    name = (name or "").lower().rstrip(".")
    # nettop hands back a name rather than an address for peers it has
    # already resolved, so the hint is sometimes sitting in the host field
    # while the reverse-DNS cache is still cold. Ignoring it there lost
    # locations the answer already contained.
    if not name and host and not re.match(r"^[0-9a-f.:]+$", host.lower()):
        name = host.lower().rstrip(".")
    if not name:
        return None
    labels = name.split(".")

    # An airport tag anywhere in the name, most specific label first: the
    # hint lives at the front (lax28s17-in-f6), never in the domain itself.
    for label in labels[:-2] or labels:
        for tag in _TAG.findall(label):
            city = CITIES.get(tag)
            if city:
                return {"lat": city[0], "lon": city[1], "city": city[2],
                        "country": city[3], "accuracy": "city"}

    # A city written out in full, which some networks prefer to a code.
    for label in labels[:-2] or labels:
        for word in _WORD.findall(label):
            city = CITIES.get(SPELLED.get(word, ""))
            if city:
                return {"lat": city[0], "lon": city[1], "city": city[2],
                        "country": city[3], "accuracy": "city"}

    # Failing that, what the domain's country says.
    tld = labels[-1] if labels else ""
    if tld in COUNTRIES and tld not in ("com", "net", "org"):
        place = COUNTRIES[tld]
        return {"lat": place[0], "lon": place[1], "city": place[2],
                "country": place[3], "accuracy": "country"}
    # co.uk, com.au and the rest carry the country one label further in.
    if len(labels) >= 2 and labels[-2] in ("co", "com", "net", "org", "ac"):
        place = COUNTRIES.get(tld)
        if place:
            return {"lat": place[0], "lon": place[1], "city": place[2],
                    "country": place[3], "accuracy": "country"}
    return None


# ---------------------------------------------------------------------------
# The lookup service.
#
# One address per request, answered from the cache ever after. Resolution
# happens on a thread of its own: a page asking where forty servers are must
# not wait on forty round trips, so the first answer is "not yet" and the map
# fills in as replies land.
# ---------------------------------------------------------------------------

SERVICE = "https://ipwho.is/"
CACHE_DDL = """
CREATE TABLE IF NOT EXISTS geo_cache (
  ip       TEXT PRIMARY KEY,
  lat      REAL,
  lon      REAL,
  city     TEXT NOT NULL DEFAULT '',
  region   TEXT NOT NULL DEFAULT '',
  country  TEXT NOT NULL DEFAULT '',
  org      TEXT NOT NULL DEFAULT '',
  ok       INTEGER NOT NULL DEFAULT 0,
  ts       INTEGER NOT NULL
);
"""

# An address does not move often, and a failure should not be retried on
# every poll for a fortnight.
KEEP = 30 * 86400
RETRY_FAILED = 6 * 3600
# Free service, so this stays well inside any sane limit and never bursts.
GAP = 1.1

_cache = {}
_queue = []
_lock = threading.Lock()
_thread = None
_own = {"at": 0.0, "where": None}
# Names to addresses. nettop hands back a name for peers it has already
# resolved, and a name cannot be looked up -- so it is turned back into an
# address first. Without this, half the connections on a busy machine were
# unplaceable for the sole reason that something else had been helpful.
_addr = {}
_IS_IP = re.compile(r"^[0-9a-f.:]+$")


def _connect():
    from . import config, db
    conn = db.connect(config.DB_PATH)
    with conn:
        conn.executescript(CACHE_DDL)
    return conn


def enabled(conn=None):
    """Whether the lookup service may be used."""
    from . import prefs
    close = False
    try:
        if conn is None:
            conn = _connect()
            close = True
        return prefs.get(conn, "geo_lookup") == "1"
    except Exception:
        return False
    finally:
        if close and conn is not None:
            conn.close()


def _row_to_place(row):
    if not row or not row[6]:
        return None
    return {"lat": row[0], "lon": row[1], "city": row[2], "region": row[3],
            "country": row[4], "org": row[5], "accuracy": "lookup"}


def _load_cache():
    """Bring what is already known into memory, once."""
    if _cache:
        return
    try:
        conn = _connect()
    except Exception:
        return
    try:
        now = int(time.time())
        for row in conn.execute(
                "SELECT ip, lat, lon, city, region, country, org, ok, ts "
                "FROM geo_cache").fetchall():
            age = now - row[8]
            if age > KEEP or (not row[7] and age > RETRY_FAILED):
                continue
            _cache[row[0]] = (row[1], row[2], row[3], row[4], row[5], row[6],
                              row[7])
    except Exception:
        pass
    finally:
        conn.close()


def _remember(ip, place):
    with _lock:
        _cache[ip] = ((place or {}).get("lat"), (place or {}).get("lon"),
                      (place or {}).get("city", ""),
                      (place or {}).get("region", ""),
                      (place or {}).get("country", ""),
                      (place or {}).get("org", ""), 1 if place else 0)
    try:
        conn = _connect()
    except Exception:
        return
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO geo_cache "
                "(ip, lat, lon, city, region, country, org, ok, ts) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (ip, (place or {}).get("lat"), (place or {}).get("lon"),
                 (place or {}).get("city", ""), (place or {}).get("region", ""),
                 (place or {}).get("country", ""), (place or {}).get("org", ""),
                 1 if place else 0, int(time.time())))
    except Exception:
        pass
    finally:
        conn.close()


def ask(ip=""):
    """One question to the service. Returns a place, or None.

    An empty address asks about this machine, which is how a program with no
    location permission finds out where it is.
    """
    url = SERVICE + (ip or "")
    request = urllib.request.Request(
        url, headers={"User-Agent": "procwatch"})
    with urllib.request.urlopen(request, timeout=12) as reply:
        answer = json.loads(reply.read(200000).decode("utf-8"))
    if not answer.get("success"):
        return None
    if answer.get("latitude") is None:
        return None
    return {"lat": answer.get("latitude"), "lon": answer.get("longitude"),
            "city": answer.get("city") or "",
            "region": answer.get("region") or "",
            "country": answer.get("country_code") or answer.get("country") or "",
            "org": (answer.get("connection") or {}).get("org") or "",
            "ip": answer.get("ip") or ip,
            "accuracy": "lookup"}


def _work():
    while True:
        with _lock:
            job = _queue.pop(0) if _queue else None
        if job is None:
            time.sleep(2)
            continue
        kind, value = job
        if kind == "name":
            # Free, local, and no waiting between them: this is the resolver
            # the machine already talks to, not the service.
            try:
                import socket
                found = socket.gethostbyname(value)
            except Exception:
                found = ""
            with _lock:
                _addr[value] = found
            continue
        try:
            _remember(value, ask(value))
        except Exception:
            # A refusal or a timeout is remembered as "not known", so the
            # same address is not asked about again on the next poll.
            _remember(value, None)
        time.sleep(GAP)


def _start():
    global _thread
    if _thread is None:
        _thread = threading.Thread(target=_work, daemon=True)
        _thread.start()


def where(host, name="", allow_lookup=True):
    """Where this peer is: the service's answer if known, the name's hint
    otherwise, and None when neither can say.

    Never blocks. An address nobody has asked about yet is queued and answered
    on a later call, which is why the map fills in rather than appearing.
    """
    if is_private(host):
        return {"lat": None, "lon": None, "city": "This network",
                "country": "", "accuracy": "local"}
    if allow_lookup and host:
        address = host
        if not _IS_IP.match(host.lower()):
            # A name: turn it back into an address, then ask about that.
            with _lock:
                address = _addr.get(host)
            if address is None:
                _queue_job(("name", host))
                address = ""
        if address and not is_private(address):
            _load_cache()
            with _lock:
                known = _cache.get(address)
            if known is not None:
                found = _row_to_place(known)
                if found:
                    return found
            else:
                _queue_job(("ip", address))
    return locate(host, name)


def _queue_job(job):
    with _lock:
        if job not in _queue and len(_queue) < 500:
            _queue.append(job)
    _start()


def own(allow_lookup=True):
    """Where this Mac is, by its public address. Cached for the day."""
    now = time.time()
    if _own["where"] is not None and now - _own["at"] < 86400:
        return _own["where"]
    if not allow_lookup:
        return None
    try:
        found = ask("")
    except Exception:
        found = None
    _own["at"] = now
    _own["where"] = found
    return found
