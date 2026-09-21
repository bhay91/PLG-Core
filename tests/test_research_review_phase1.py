import unittest
from plg_core.intake.research_review import review_from_text, review_from_package
from plg_core.intake.research_import import ResearchImportPackage

class ResearchReviewPhase1Tests(unittest.TestCase):
    def test_text_pdf_extraction_is_provisional_and_explicit(self):
        review = review_from_text('Customer: Bobby\nManufacturer: Komatsu\nModel: PC210\nPIN: ABC123\nPart: Hydraulic pump Qty 2')
        self.assertEqual(review['status'], 'PROVISIONAL')
        self.assertEqual(review['customer']['name'], 'Bobby')
        self.assertEqual(review['machine']['model'], 'PC210')
        self.assertEqual(review['machine']['identifiers'][0]['value'], 'ABC123')
        self.assertEqual(review['requested_needs'][0]['quantity'], '2')
        self.assertIn('research_evidence', review)

    def test_package_maps_to_same_provisional_shape(self):
        package = ResearchImportPackage.model_validate({
            'package_id':'pkg-1','source_pdf':{'filename':'r.pdf','sha256':'a'*64},
            'target':{'mode':'NEW_JOB'}, 'customer':{'name':'Bobby','company':''},
            'machine':{'reference':'m1','manufacturer':'Komatsu','model':'PC210','asset_type':'machine'},
            'requested_needs':[{'reference':'n1','original_wording':'Hydraulic pump','quantity':1}],
            'research_options':[{'reference':'o1','requested_need_reference':'n1','description':'Pump','quantity':1,'source_evidence':{'pdf_reference':'r.pdf'}}]
        })
        review = review_from_package(package)
        self.assertEqual(review['status'], 'PROVISIONAL')
        self.assertEqual(review['requested_needs'][0]['original_wording'], 'Hydraulic pump')
        self.assertEqual(review['part_evidence'][0]['need'], 'n1')
        self.assertEqual(review['supplier_evidence'][0]['need'], 'n1')
        self.assertEqual(review['source']['sha256'], 'a'*64)

    def test_normalizer_has_no_authoritative_service_dependency(self):
        import plg_core.intake.research_review as module
        self.assertFalse(any(name in module.__dict__ for name in ('create_customer','create_machine','create_supplier','create_quote')))
