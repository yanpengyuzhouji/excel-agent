import unittest
from types import SimpleNamespace
from app.service.attachment_policy import review_issues


class AttachmentPolicyTests(unittest.TestCase):
    def response(self, warnings=(), errors=(), sheets=()):
        return SimpleNamespace(validation={'warnings': list(warnings), 'errors': list(errors)}, sheets=list(sheets))

    def test_ordinary_warnings_auto_confirm(self):
        warnings = [{'code': c} for c in ['MIXED_NUMERIC_PLACEHOLDER', 'HIDDEN_ROWS', 'MERGED_CELLS', 'FORMULAS']]
        self.assertEqual(review_issues(self.response(warnings)), ([], []))

    def test_unknown_and_empty_headers_require_review(self):
        for code in ['EMPTY_HEADER', 'NEW_UNKNOWN_WARNING']:
            self.assertEqual(len(review_issues(self.response([{'code': code}]))[1]), 1)

    def test_verified_nonbusiness_notes_do_not_require_review(self):
        warning = {'code': 'NONBUSINESS_ROWS', 'auto_confirm_safe': True}
        self.assertEqual(([], []), review_issues(self.response([warning])))

    def test_unverified_nonbusiness_rows_still_require_review(self):
        for safe in (None, False, 'true'):
            warning = {'code': 'NONBUSINESS_ROWS', 'auto_confirm_safe': safe}
            self.assertEqual([warning], review_issues(self.response([warning]))[1])

    def test_missing_formula_cache_requires_review(self):
        sheet = {'name': 'Sheet1', 'enabled': True, 'kind': 'data', 'formulas': {'A2': {'cached_value': None}}}
        self.assertEqual(review_issues(self.response(sheets=[sheet]))[1][0]['code'], 'FORMULA_CACHE_MISSING')
        sheet['formulas']['A2']['cached_value'] = 0
        self.assertEqual(review_issues(self.response(sheets=[sheet])), ([], []))

    def test_errors_are_never_accepted_as_warnings(self):
        self.assertTrue(review_issues(self.response(errors=[{'code': 'NO_DATA_SHEET'}]))[0])
