from django.test import TestCase

from projects.models import Project
from sites.models import Site


class ProjectModelTests(TestCase):
    def test_project_collects_sites(self):
        project = Project.objects.create(name="Downtown")
        site_a = Site.objects.create(
            project=project,
            name="Lot A",
        )
        site_b = Site.objects.create(
            project=project,
            name="Lot B",
        )
        self.assertEqual(project.sites.count(), 2)
        self.assertEqual(set(project.sites.all()), {site_a, site_b})
        self.assertIn(project.name, str(site_a))

    def test_country_defaults_to_sa(self):
        project = Project.objects.create(name="Riyadh")
        self.assertEqual(project.country, "SA")
        self.assertEqual(project.seller_name, "")
        self.assertEqual(project.vat_number, "")
