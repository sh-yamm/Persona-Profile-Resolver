"""Offline tests (no network): parsing, features and scoring on synthetic data."""

import json

from enrich import features, persona, scorer
from enrich.features import Candidate
from enrich.linkedin import parse_profile_html
from enrich.search import canonical_profile, parse_snippet

PROFILE_HTML = """<html><head>
<title>Jane Q Doe - Acme Robotics | LinkedIn</title>
<meta property="og:title" content="Jane Q Doe - Acme Robotics | LinkedIn">
<meta name="description" content="Experience: Acme Robotics · Education: Example University · Location: Austin · 500+ connections on LinkedIn.">
<meta property="og:image" content="https://media.licdn.com/dms/image/x/profile-displayphoto.jpg">
<script type="application/ld+json">{"@context":"http://schema.org","@graph":[
 {"@type":"Person","name":"Jane Q Doe","address":{"@type":"PostalAddress","addressCountry":"US","addressLocality":"Austin, Texas, United States"},
  "worksFor":[{"@type":"Organization","name":"Acme Robotics","url":"https://www.linkedin.com/company/acme-robotics"}],
  "jobTitle":["*** ****"],"image":{"@type":"ImageObject","contentUrl":"https://media.licdn.com/dms/image/x/photo.jpg"}},
 {"@type":"DiscussionForumPosting","author":{"@type":"Person","url":"https://www.linkedin.com/in/janeqdoe"},"text":"Shipping our new warehouse robot today"}
]}</script></head><body><h1>Jane Q Doe</h1>
<div data-section="websites"><a href="https://www.linkedin.com/redir/redirect?url=https%3A%2F%2Facmerobotics%2Eio&urlhash=x">Company Website</a></div>
</body></html>"""

RAW = {"name": "Jane Doe (Acme)", "image": None,
       "intro": "Head of Ops @ Acme Robotics (https://acmerobotics.io) - Austin, TX",
       "timezone": "America/Chicago", "company_industry": None, "company_size": None,
       "social_profile": ["https://twitter.com/janedoe_ops"]}


def test_name_parsing():
    assert persona.parse_name("DamionW")["last_initial"] == "W"
    n = persona.parse_name("Uriel S.")
    assert (n["first"], n["last_initial"], n["complete"]) == ("Uriel", "S", False)
    n = persona.parse_name("Eric Doty (Superpath)")
    assert (n["first"], n["last"], n["hint"]) == ("Eric", "Doty", "Superpath")


def test_intro_parsing():
    p = persona.build(RAW)
    assert "Acme Robotics" in p.companies
    assert p.domains == ["acmerobotics.io"]
    assert p.country == "US" and p.city_hint == "Austin"
    assert p.twitter == "janedoe_ops"
    assert any("Head of Ops" in t for t in p.titles)


def test_tools_are_not_companies():
    p = persona.build({"name": "A B", "intro": "3x CEO. Tools: Salesforce, Hubspot, Zoom"})
    assert not ({"Salesforce", "Hubspot", "Zoom"} & set(p.companies + p.weak_companies))


def test_profile_parse_masks_and_redirects():
    prof = parse_profile_html(PROFILE_HTML, "https://www.linkedin.com/in/janeqdoe")
    assert prof.ok and prof.name == "Jane Q Doe"
    assert prof.country == "US" and prof.companies[0] == "Acme Robotics"
    assert prof.headline == ""                       # masked "*** ****" is dropped
    assert prof.websites == ["https://acmerobotics.io"]
    assert prof.posts and "warehouse robot" in prof.posts[0]


def test_snippet_and_url_canonicalisation():
    s = parse_snippet("Jane Doe – Head of Ops – Acme Robotics | LinkedIn",
                      "Experience: Acme Robotics · Location: Austin · 500+ connections")
    assert (s["name"], s["headline"], s["company"], s["location"]) == ("Jane Doe", "Head of Ops", "Acme Robotics", "Austin")
    assert canonical_profile("https://uk.linkedin.com/in/Jane-Doe-12ab/?trk=x") == "https://www.linkedin.com/in/jane-doe-12ab"
    s = parse_snippet("Jane Doe - LinkedIn", "Jane Doe Design Director @ View Source Brooklyn, New York, United States "
                      "625 followers 500+ connections See your mutual connections View Source")
    assert s["location"].endswith("New York, United States") and s["company"] == "View Source"
    # junk scraped from page markup must never become a fetch (it triggers HTTP 999)
    assert canonical_profile("https://www.linkedin.com/in/carrie-chan-%2943%3At751%2Chello") is None
    assert canonical_profile("https://www.linkedin.com/in/carrie-chan-%7Cwww.linkedin.com") is None


def _cand(html=PROFILE_HTML, face=None):
    c = Candidate(url="https://www.linkedin.com/in/janeqdoe", sources={"search:tight"}, best_rank=1)
    c.profile = parse_profile_html(html, c.url)
    c.face_sim = face
    return c


def test_true_match_scores_high_and_explains():
    p = persona.build(RAW)
    c = _cand(face=0.62)
    features.extract(p, c, {})
    assert c.levels["name"] == "full"
    assert c.levels["company"] == "strong"
    assert c.levels["domain"] == "match"
    assert c.levels["location"] == "country_stated"
    prob = scorer.probability(scorer.raw_score(c.levels))
    lo, hi = scorer.interval(c.levels, 0)
    assert prob > 0.95 and lo <= round(prob, 3) <= hi
    fv = scorer.fields_validated(c.levels)
    assert fv["count"] >= 4 and fv["available"] >= fv["count"]


def test_wrong_person_scores_low():
    p = persona.build(RAW)
    other = (PROFILE_HTML.replace("Jane Q Doe", "John Smith").replace("Acme Robotics", "Globex")
             .replace("acmerobotics", "globex").replace("acme-robotics", "globex"))
    c = _cand(other, face=0.05)
    features.extract(p, c, {})
    assert c.levels["name"] == "mismatch"
    assert scorer.probability(scorer.raw_score(c.levels)) < 0.01


def test_namesake_penalty():
    p = persona.build({"name": "Jane Doe", "intro": None})
    c = _cand()
    features.extract(p, c, {})
    unique = scorer.raw_score(c.levels, 0)
    common = scorer.raw_score(c.levels, 6)
    assert common < unique


def test_rare_intro_word_matches_company():
    from enrich.pipeline import _distinctive
    p = persona.build({"name": "Jane Doe", "intro": "software developer, acmeflowcraft, python"})
    p.rare_terms = _distinctive(p)
    c = Candidate(url="https://www.linkedin.com/in/jdoe",
                  snippets=[parse_snippet("Jane Doe - AcmeFlow | LinkedIn", "Experience: AcmeFlow")])
    features.extract(p, c, {})
    assert c.levels["company"] == "strong"


def test_missing_data_is_not_mismatch():
    p = persona.build({"name": "Jane Doe"})
    c = _cand()
    features.extract(p, c, {})
    assert c.levels["company"] is None and c.levels["face"] is None
