"""European search groups and metadata-only place exclusions."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import search_prefs
import job_filters
import collectors


class EuropeSearchTest(unittest.TestCase):
    def prefs(self, **changes):
        return dict(enabled=True, roles=['Software Engineer'], job_types=['full_time'],
                    locations=['Switzerland'], europe_enabled=True,
                    europe_locations=['Sweden', 'Portugal'],
                    europe_job_types=['graduate', 'intern'], **changes)

    def test_independent_group_and_provider_countries(self):
        plan = search_prefs.searches({'search_preferences': self.prefs()})
        rows = plan['linkedin']
        self.assertTrue(any(r['l'] == 'Sweden' and 'Graduate' in r['q'] for r in rows))
        self.assertFalse(any(r['l'] == 'Portugal' and r['generated'] == 'full_time' for r in rows))
        self.assertTrue(any(r['country'] == 'PT,SE' for r in plan['freehire']))
        self.assertTrue(any(r['l'] == 'Switzerland' and r['q'] == 'Software Engineer' for r in rows))

    def test_disable_and_deduplicate(self):
        p = self.prefs()
        p.update(europe_locations=['Switzerland'], europe_job_types=['full_time'])
        self.assertEqual(len(search_prefs.searches({'search_preferences': p})['linkedin']), 1)
        p.update(europe_enabled=False, europe_locations=['Sweden'])
        self.assertFalse(any(r['l'] == 'Sweden' for r in search_prefs.searches({'search_preferences': p})['linkedin']))

    def test_validation_and_large_region(self):
        p = self.prefs(excluded_locations=['Sofia', 'sofia', '  '])
        p['europe_locations'] = sum(search_prefs.EUROPE_REGIONS.values(), [])
        block, error = search_prefs.clean(p)
        self.assertIsNone(error)
        self.assertEqual(block['excluded_locations'], ['Sofia'])
        self.assertIsNotNone(search_prefs.clean(dict(p, europe_job_types=['invalid']))[1])

    def test_excluded_location_skips_detail_and_uses_word_boundaries(self):
        opts = job_filters.settings({'search_preferences': {'excluded_locations': ['Sofia']}})
        self.assertIsNotNone(job_filters.location_hit('Sofia, Bulgaria', opts))
        self.assertIsNone(job_filters.location_hit('Sofiane', opts))
        self.assertIsNone(job_filters.location_hit('', opts))
        with patch.object(job_filters, 'load_settings', return_value=opts), patch.object(collectors.subprocess, 'run') as run:
            status, note, _ = collectors.screen({'location': 'Sofia', 'title': 'Graduate Engineer', 'portal': 'linkedin-search'}, lambda *a: None)
        self.assertEqual(status, 'gate')
        self.assertIn('excluded location', note)
        run.assert_not_called()
