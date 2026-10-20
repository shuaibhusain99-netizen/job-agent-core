"""Scoring rules on realistic ads. Run: python -m unittest discover -s tests -t ."""
import json
import unittest
from datetime import date, timedelta
from pathlib import Path

from agent.score import score, required_years, find_salary, find_apply_email
from agent.util import job

ROOT = Path(__file__).resolve().parents[1]
PROFILE = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
LONG = " Civil construction works including earthworks, drainage, roads, concrete, quality and safety on site." * 4


def run(title, company="ACME", location="", country="", desc="", source="LinkedIn", posted=""):
    j = job(title, company, location, "https://example.org/" + title.replace(" ", "-"), desc, source, country, posted)
    s, v, why = score(j, PROFILE)
    return s, v, j, "; ".join(why)


class Matches(unittest.TestCase):
    def assertVerdict(self, res, allowed):
        s, v, j, why = res
        self.assertIn(v, allowed, f"{j['title']} ({j['country']}): got {v} {s} - {why}")

    # --- good matches in the Gulf
    def test_civil_engineer_gulf(self):
        self.assertVerdict(run("Civil Engineer", "Al Shirawi Contracting", "Riyadh", "Saudi Arabia",
                               "Civil engineer for road and drainage works. 3-5 years experience in infrastructure projects." + LONG), {"apply"})

    def test_site_engineer_listing_only(self):
        self.assertVerdict(run("Site Engineer", "ACME Contracting", "Dubai", "United Arab Emirates"), {"apply"})

    def test_civil_inspector_is_kept(self):
        self.assertVerdict(run("Civil Inspector", "AECOM", "Riyadh", "Saudi Arabia",
                               "Inspect civil works, concrete, earthworks and utilities on site; review drawings and ITPs." * 4), {"apply", "maybe"})

    def test_document_controller_starter(self):
        s, v, j, why = run("Document Controller", "ACWA Power", "Riyadh", "Saudi Arabia",
                           "Document control for power and water plant projects; EDMS, transmittals, 2-4 years experience.")
        self.assertEqual(v, "apply", why)

    def test_listing_only_other_roles_survive_until_the_full_ad(self):
        for t in ("Quantity Surveyor", "HSE Officer", "Storekeeper", "Civil Inspector"):
            self.assertVerdict(run(t, "Unknown", "Doha", "Qatar"), {"apply", "maybe"})

    def test_lmia_canada_counts_as_sponsoring(self):
        self.assertVerdict(run("Civil Engineer", "City of Regina", "Regina SK", "Canada",
                               "Municipal civil engineer for watermain and roads. LMIA approved position, temporary foreign workers welcome." * 3), {"apply"})

    # --- other industries / disciplines
    def test_generic_titles_from_other_industries_are_out(self):
        self.assertVerdict(run("Packing Floor Maintenance Engineer", "McLean Farms", "Sydney NSW", "Australia",
                               "Maintain packing lines in our poultry processing plant. Mechanical trade background. " * 5), {"reject"})
        self.assertVerdict(run("PMC I&C Construction Engineer", "Wood", "Abu Dhabi", "United Arab Emirates",
                               "Instrumentation and control construction supervision. " * 8), {"reject"})
        self.assertVerdict(run("Project Engineer (Nitrogen)", "Contract Resources", "Perth WA", "Australia"), {"reject", "maybe"})
        self.assertVerdict(run("Software Engineer", "X", "Dubai", "United Arab Emirates"), {"reject"})

    def test_generic_title_without_context_is_review(self):
        self.assertVerdict(run("Project Engineer", "Teksky LLC", "Dubai", "United Arab Emirates"), {"maybe", "reject"})
        self.assertVerdict(run("Project Engineer", "Gardinia Contracting", "Dubai", "United Arab Emirates"), {"apply"})

    def test_business_analyst_outside_construction(self):
        self.assertVerdict(run("Business Analyst", "Westpac", "Sydney NSW", "Australia",
                               "Banking business analyst for lending products, requirements and stakeholder workshops. " * 6), {"reject"})

    # --- seniority, nationality, age
    def test_senior_without_fitting_years_is_review(self):
        self.assertVerdict(run("Senior Civil Engineer", "Worley", "Abu Dhabi", "United Arab Emirates", LONG), {"maybe", "reject"})

    def test_far_more_years_is_rejected(self):
        self.assertVerdict(run("Civil Engineer", "X", "Riyadh", "Saudi Arabia", "Civil engineer with minimum 10 years experience." + LONG), {"reject"})

    def test_company_history_years_are_not_requirements(self):
        self.assertEqual(required_years("A contractor with over 25 years of experience in the Kingdom. We need a site engineer."), (None, None))
        self.assertEqual(required_years("Candidates should have 3-5 years of experience in roads."), (3, 5))

    def test_nationals_only(self):
        self.assertVerdict(run("Civil Engineer (Saudi Nationals Only)", "X", "Riyadh", "Saudi Arabia"), {"reject"})
        self.assertVerdict(run("Civil Engineer", "X", "Riyadh", "Saudi Arabia", "Open to Saudi nationals only." + LONG), {"reject"})
        self.assertVerdict(run("Civil Engineer (GCC Nationals)", "X", "Riyadh", "Saudi Arabia"), {"apply", "maybe"})

    def test_old_ads_are_rejected(self):
        old = (date.today() - timedelta(days=80)).isoformat()
        self.assertVerdict(run("Civil Engineer", "X", "Riyadh", "Saudi Arabia", posted=old), {"reject"})

    # --- work visas outside the GCC
    def test_australia_citizens_only(self):
        self.assertVerdict(run("Graduate Civil Engineer", "GHD", "Brisbane QLD", "Australia",
                               "Applicants must be Australian citizens or permanent residents." + LONG), {"reject"})

    def test_australia_without_sponsorship_is_review(self):
        self.assertVerdict(run("Site Engineer", "Seymour Whyte", "Brisbane QLD", "Australia", LONG), {"maybe"})
        self.assertVerdict(run("Site Engineer", "Seymour Whyte", "Brisbane QLD", "Australia", LONG + " No visa sponsorship available."), {"maybe", "reject"})

    def test_australia_with_sponsorship_is_apply(self):
        self.assertVerdict(run("Site Engineer", "Seymour Whyte", "Brisbane QLD", "Australia",
                               LONG + " Visa sponsorship available for the right candidate."), {"apply"})

    def test_country_switched_off(self):
        self.assertVerdict(run("Civil Engineer", "X", "Manama, Bahrain", "Bahrain"), {"reject"})
        self.assertVerdict(run("Civil Engineer", "X", "Cairo, Egypt", "Other"), {"reject"})


class Extraction(unittest.TestCase):
    def test_salary(self):
        self.assertEqual(find_salary("Salary: SAR 8,000 - 10,000 per month plus housing"), "SAR 8,000 - 10,000")
        self.assertEqual(find_salary("Established in 2005, the company has 2000 staff."), "")

    def test_email(self):
        self.assertEqual(find_apply_email("Send your CV to hr [at] acme [dot] com today"), "hr@acme.com")
        self.assertEqual(find_apply_email("noreply@linkedin.com logo@2x.png"), "")


if __name__ == "__main__":
    unittest.main()
