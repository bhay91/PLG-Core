from decimal import Decimal
import unittest

from pydantic import ValidationError

from plg_core.intake.research_import import ResearchImportPackage


def package(**overrides):
    value = {
        "package_id": "CHATGPT-RESEARCH-Bobby-001",
        "source_pdf": {"filename": "bobby.pdf", "sha256": "a" * 64},
        "target": {"mode": "EXISTING_JOB", "job_number": "PPS-J-0001"},
        "customer": {"name": "Bobby", "company": "Seals Construction"},
        "machine": {
            "reference": "bobby-truck", "manufacturer": "International", "model": "5600i",
            "asset_type": "vehicle", "identifiers": [{"type": "AUTOMOTIVE_VIN", "value": "1HTAAAA", "primary": True}],
        },
        "requested_needs": [{"reference": "need-001", "original_wording": "Brake rotor", "quantity": 2, "machine_reference": "bobby-truck"}],
        "estimated_inbound_freight": {"amount": "225.00", "currency": "USD", "status": "ESTIMATED"},
        "research_options": [{
            "reference": "option-001", "requested_need_reference": "need-001", "description": "Brake rotor",
            "supplier": "Supplier", "supplier_url": "https://supplier.example/item", "quantity": 2,
            "supplier_base_unit_price": "100.00", "supplier_base_extended_price": "200.00",
            "tax_rate_percent": "18.00", "tax_amount": "36.00", "tax_inclusive_unit_cost": "118.00",
            "tax_inclusive_extended_cost": "236.00", "source_evidence": {
                "pdf_reference": "bobby.pdf", "page": 2, "source_urls": ["https://supplier.example/item"]
            },
        }],
    }
    value.update(overrides)
    return value


class ResearchImportSchemaTests(unittest.TestCase):
    def test_valid_package_preserves_decimal_and_freight_fields(self):
        result = ResearchImportPackage.model_validate(package())
        self.assertEqual(result.target.job_number, "PPS-J-0001")
        self.assertEqual(result.requested_needs[0].quantity, Decimal("2"))
        self.assertEqual(result.research_options[0].tax_inclusive_extended_cost, "236.00")
        self.assertEqual(result.estimated_inbound_freight.amount, "225.00")

    def test_markdown_or_http_urls_are_rejected(self):
        with self.assertRaises(ValidationError):
            ResearchImportPackage.model_validate(package(research_options=[{
                **package()["research_options"][0], "supplier_url": "[supplier](https://supplier.example/item)"
            }]))
        with self.assertRaises(ValidationError):
            ResearchImportPackage.model_validate(package(research_options=[{
                **package()["research_options"][0], "supplier_url": "http://supplier.example/item"
            }]))

    def test_research_options_must_reference_existing_need(self):
        option = {**package()["research_options"][0], "requested_need_reference": "missing"}
        with self.assertRaises(ValidationError):
            ResearchImportPackage.model_validate(package(research_options=[option]))

    def test_existing_asset_and_identifier_types_are_accepted(self):
        for asset_type in ("vehicle", "machine", "engine", "marine", "generator", "trailer", "component", "other"):
            value = package()
            value["machine"]["asset_type"] = asset_type
            self.assertEqual(ResearchImportPackage.model_validate(value).machine.asset_type, asset_type)
        for identifier_type in ("AUTOMOTIVE_VIN", "JDM_FRAME", "JDM_CHASSIS", "MODEL_CODE", "PIN", "MACHINE_SERIAL", "ENGINE_SERIAL", "COMPONENT_SERIAL", "OTHER_IDENTIFIER", "UNKNOWN"):
            value = package()
            value["machine"]["identifiers"][0]["type"] = identifier_type
            self.assertEqual(ResearchImportPackage.model_validate(value).machine.identifiers[0].type, identifier_type)

    def test_unsupported_asset_and_identifier_types_are_rejected(self):
        value = package()
        value["machine"]["asset_type"] = "spaceship"
        with self.assertRaises(ValidationError):
            ResearchImportPackage.model_validate(value)

    def test_target_modes_are_explicit(self):
        value = package(target={"mode": "NEW_JOB"})
        result = ResearchImportPackage.model_validate(value)
        self.assertEqual(result.target.mode, "NEW_JOB")
        with self.assertRaises(ValidationError):
            ResearchImportPackage.model_validate(package(target={"mode": "EXISTING_JOB"}))
        with self.assertRaises(ValidationError):
            ResearchImportPackage.model_validate(package(target={"mode": "NEW_JOB", "job_number": "PPS-J-0001"}))
        value = package()
        value["machine"]["identifiers"][0]["type"] = "MADE_UP_IDENTIFIER"
        with self.assertRaises(ValidationError):
            ResearchImportPackage.model_validate(value)

    def test_unknown_authoritative_pricing_fields_are_rejected(self):
        value = package()
        value["research_options"][0]["customer_unit_price"] = "999.00"
        with self.assertRaises(ValidationError):
            ResearchImportPackage.model_validate(value)


if __name__ == "__main__":
    unittest.main()
