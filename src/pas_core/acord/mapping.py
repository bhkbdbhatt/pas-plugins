"""Translation from internal domain models to ACORD NGDS payloads.

Every plugin that writes to or reads from a PAS uses these helpers so the wire
format is identical regardless of which entry point initiated the transaction -
this is what makes a quote created by the embedded widget and a quote created by
an AI agent through MCP indistinguishable to the carrier's core system.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from pas_core.acord import models
from pas_core.acord.schema import validate_payload
from pas_core.acord.transaction import (
    BusinessArea,
    EnvelopeDirection,
    TransactionContext,
    TransactionEnvelope,
    TransactionTypeCode,
    definition_for,
)
from pas_core.errors import ValidationError

TX_FOR_COVERAGE: dict[models.CoverageType, TransactionTypeCode] = {
    models.CoverageType.FIXED_INDEXED_ANNUITY: TransactionTypeCode.TX201_ANNUITY_APPLICATION_SUBMISSION,
    models.CoverageType.VARIABLE_ANNUITY: TransactionTypeCode.TX201_ANNUITY_APPLICATION_SUBMISSION,
    models.CoverageType.IMMEDIATE_ANNUITY: TransactionTypeCode.TX201_ANNUITY_APPLICATION_SUBMISSION,
    models.CoverageType.DEFERRED_ANNUITY: TransactionTypeCode.TX201_ANNUITY_APPLICATION_SUBMISSION,
}

TX_FOR_POLICY_STATUS: dict[models.PolicyStatus, TransactionTypeCode] = {
    models.PolicyStatus.QUOTED: TransactionTypeCode.TX101_LIFE_APPLICATION_SUBMISSION,
    models.PolicyStatus.SUBMITTED: TransactionTypeCode.TX101_LIFE_APPLICATION_SUBMISSION,
    models.PolicyStatus.UNDERWRITING: TransactionTypeCode.TX103_LIFE_APPLICATION_DECISION,
    models.PolicyStatus.REFERRED: TransactionTypeCode.TX103_LIFE_APPLICATION_DECISION,
    models.PolicyStatus.ACCEPTED: TransactionTypeCode.TX102_LIFE_APPLICATION_ACCEPTANCE,
    models.PolicyStatus.DECLINED: TransactionTypeCode.TX103_LIFE_APPLICATION_DECISION,
    models.PolicyStatus.ISSUED: TransactionTypeCode.TX105_LIFE_POLICY_ISSUE,
    models.PolicyStatus.ACTIVE: TransactionTypeCode.TX105_LIFE_POLICY_ISSUE,
    models.PolicyStatus.LAPSED: TransactionTypeCode.TX120_LIFE_POLICY_LAPSE_REINSTATEMENT,
    models.PolicyStatus.PAID_UP: TransactionTypeCode.TX106_LIFE_POLICY_TRANSACTION,
    models.PolicyStatus.SURRENDERED: TransactionTypeCode.TX106_LIFE_POLICY_TRANSACTION,
    models.PolicyStatus.TERMINATED: TransactionTypeCode.TX106_LIFE_POLICY_TRANSACTION,
    models.PolicyStatus.EXPIRED: TransactionTypeCode.TX106_LIFE_POLICY_TRANSACTION,
}


def business_area_for(coverage_types: list[models.CoverageType]) -> BusinessArea:
    """Annuity coverage types map to the annuity business area; life to life."""
    annuity_codes = {
        models.CoverageType.FIXED_INDEXED_ANNUITY,
        models.CoverageType.VARIABLE_ANNUITY,
        models.CoverageType.IMMEDIATE_ANNUITY,
        models.CoverageType.DEFERRED_ANNUITY,
    }
    if coverage_types and all(c in annuity_codes for c in coverage_types):
        return BusinessArea.ANNUITY
    return BusinessArea.LIFE


def select_transaction_code(
    *,
    coverage_types: list[models.CoverageType] | None = None,
    status: models.PolicyStatus | None = None,
    override: str | None = None,
) -> TransactionTypeCode:
    """Pick the NGDS transaction code for a business intent."""
    if override:
        return definition_for(override).code
    if status is not None:
        code = TX_FOR_POLICY_STATUS.get(status)
        if code is None:
            raise ValidationError(
                f"No ACORD transaction code mapped for policy status '{status}'",
                status=str(status),
            )
        return code
    if coverage_types:
        return TX_FOR_COVERAGE.get(
            coverage_types[0], TransactionTypeCode.TX101_LIFE_APPLICATION_SUBMISSION
        )
    return TransactionTypeCode.TX101_LIFE_APPLICATION_SUBMISSION


def person_to_acord(person: models.Person) -> dict[str, Any]:
    """Person -> NGDS party object."""
    payload: dict[str, Any] = {
        "PartyOccurrenceID": person.party_id,
        "Person": {
            "FirstName": person.first_name,
            "LastName": person.last_name,
            "BirthDate": person.date_of_birth.isoformat(),
            "GenderCode": str(person.gender.value),
        },
    }
    if person.middle_name:
        payload["Person"]["MiddleName"] = person.middle_name
    if person.suffix:
        payload["Person"]["Suffix"] = person.suffix
    if person.ssn_last4:
        # Full SSNs are deliberately not carried by NGDS payloads in this suite.
        payload["Person"]["SSNLast4"] = person.ssn_last4
    if person.tobacco_use is not None:
        payload["Person"]["TobaccoUseIndicator"] = person.tobacco_use
    if person.preferred_language:
        payload["Person"]["PreferredLanguageCode"] = person.preferred_language
    if person.address:
        address = person.address
        payload["Address"] = {
            "AddressLine1": address.line1,
            "AddressCity": address.city,
            "AddressStateCode": address.state,
            "PostalCode": address.postal_code,
            "CountryCode": address.country,
        }
        if address.line2:
            payload["Address"]["AddressLine2"] = address.line2
    if person.contact:
        contact = person.contact
        payload["ContactInfo"] = {
            k: v
            for k, v in (
                ("TelephoneNumber", contact.telephone),
                ("EmailAddress", contact.email),
            )
            if v
        }
    return payload


def coverage_to_acord(coverage: models.Coverage) -> dict[str, Any]:
    """Coverage -> NGDS coverage object."""
    payload: dict[str, Any] = {
        "CoverageID": coverage.coverage_id,
        "CoverageTypeCode": str(coverage.coverage_type.value),
        "RiderIndicator": coverage.is_rider,
        "GuaranteedIndicator": coverage.is_guaranteed,
    }
    if coverage.face_amount is not None:
        payload["FaceAmount"] = coverage.face_amount
    if coverage.benefit_period_years:
        payload["BenefitPeriodYears"] = coverage.benefit_period_years
    if coverage.benefit_period_months:
        payload["BenefitPeriodMonths"] = coverage.benefit_period_months
    if coverage.premium_period_years:
        payload["PremiumPeriodYears"] = coverage.premium_period_years
    if coverage.premium_to_age:
        payload["PremiumToAge"] = coverage.premium_to_age
    if coverage.waiting_period_months is not None:
        payload["WaitingPeriodMonths"] = coverage.waiting_period_months
    if coverage.rate_class:
        payload["RateClass"] = coverage.rate_class
    return payload


def policy_to_acord_content(policy: models.Policy) -> dict[str, Any]:
    """Policy -> NGDS ``Content`` payload for a life policy transaction."""
    content: dict[str, Any] = {
        "PolicyNumber": policy.policy_id,
        "ProductCode": policy.product_code,
        "PolicyStatusCode": str(policy.status.value),
        "StateCode": policy.state_of_issue,
        "FaceAmount": policy.face_amount,
        "CurrencyCode": policy.currency,
        "LineOfBusiness": policy.lob,
    }
    for key, value in (
        ("IssueDate", policy.issue_date),
        ("EffectiveDate", policy.effective_date),
        ("ExpirationDate", policy.expiration_date),
    ):
        if value:
            content[key] = value.isoformat()
    if policy.annualised_premium:
        content["AnnualisedPremiumAmount"] = policy.annualised_premium
        content["PaymentModeCode"] = str(policy.payment_mode.value)
    if policy.master_group:
        content["MasterGroupIndicator"] = policy.master_group
    if policy.coverages:
        content["Coverage"] = [coverage_to_acord(c) for c in policy.coverages]
    if policy.parties:
        content["PartyRole"] = [
            {
                "PartyOccurrenceID": role.party_id,
                "RelationshipCode": str(role.relationship.value),
                **({"SharePercent": role.share_percent} if role.share_percent is not None else {}),
                **({"PrimaryIndicator": role.is_primary} if role.is_primary else {}),
            }
            for role in policy.parties
        ]
    if policy.events:
        content["LifeEvent"] = [
            {
                "EventID": event.event_id,
                "EventTypeCode": event.event_type,
                "EffectiveDate": event.effective_date.isoformat(),
                "EventStatusCode": str(event.status.value),
                **({"EventAmount": event.amount} if event.amount is not None else {}),
            }
            for event in policy.events
        ]
    return content


def to_acord_payload(policy: models.Policy) -> dict[str, Any]:
    """Bare NGDS content payload without the envelope."""
    return policy_to_acord_content(policy)


def to_acord_envelope(
    policy: models.Policy,
    context: TransactionContext,
    *,
    override_code: str | None = None,
    status_block: dict[str, Any] | None = None,
    extension: dict[str, Any] | None = None,
) -> TransactionEnvelope:
    """Wrap a policy in a validated NGDS transaction envelope.

    Raises a catalogue ``ValidationError`` when the resulting envelope does not
    satisfy the published transaction schema, which prevents malformed messages
    from ever reaching a PAS.
    """
    code = select_transaction_code(
        coverage_types=[c.coverage_type for c in policy.coverages],
        status=policy.status,
        override=override_code,
    )
    definition = definition_for(code.value)
    envelope = TransactionEnvelope(
        SenderID=context.sender_id,
        ReceiverID=context.receiver_id,
        MessageID=context.correlation_id,
        TransactionTypeCode=code.value,
        MessageDateTime=datetime.now(UTC).isoformat(timespec="seconds"),
        BusinessAreaCode=str(
            business_area_for([c.coverage_type for c in policy.coverages]).value
        )
        if policy.coverages
        else str(context.business_area.value),
        SecurityLevel=context.security_level,
        TestMode=context.test_mode,
        Status=status_block,
        Content=policy_to_acord_content(policy),
        Extension={"sourceRef": definition.source_ref, **(extension or {})},
    )
    validate_payload("TransactionEnvelope", envelope.to_dict())
    if definition.direction is EnvelopeDirection.REQUEST and envelope.is_response:
        msg = f"code {code.value} is a request but the envelope was marked as a response"
        raise ValidationError(msg, code=code.value)
    return envelope


def parse_party(payload: dict[str, Any]) -> models.Person:
    """NGDS party object -> :class:`Person`."""
    person = payload.get("Person", {})
    address_block = payload.get("Address") or {}
    contact_block = payload.get("ContactInfo") or {}
    address = None
    if address_block:
        address = models.Address(
            line1=address_block["AddressLine1"],
            line2=address_block.get("AddressLine2"),
            city=address_block["AddressCity"],
            state=address_block["AddressStateCode"],
            postal_code=address_block["PostalCode"],
            country=address_block.get("CountryCode", "US"),
        )
    contact = None
    if contact_block:
        contact = models.Contact(
            telephone=contact_block.get("TelephoneNumber"),
            email=contact_block.get("EmailAddress"),
        )
    gender_raw = person.get("GenderCode", "U")
    try:
        gender = models.Gender(gender_raw)
    except ValueError:
        gender = models.Gender.UNKNOWN
    return models.Person(
        party_id=payload["PartyOccurrenceID"],
        first_name=person["FirstName"],
        last_name=person["LastName"],
        middle_name=person.get("MiddleName"),
        suffix=person.get("Suffix"),
        date_of_birth=date.fromisoformat(str(person["BirthDate"])[:10]),
        gender=gender,
        ssn_last4=person.get("SSNLast4"),
        address=address,
        contact=contact or None,
        tobacco_use=person.get("TobaccoUseIndicator"),
        preferred_language=person.get("PreferredLanguageCode"),
    )


def parse_policy_content(content: dict[str, Any]) -> models.Policy:
    """NGDS ``Content`` payload -> :class:`Policy`."""
    coverages = [
        models.Coverage(
            coverage_id=c["CoverageID"],
            coverage_type=models.CoverageType(c["CoverageTypeCode"]),
            face_amount=c.get("FaceAmount"),
            benefit_period_years=c.get("BenefitPeriodYears"),
            benefit_period_months=c.get("BenefitPeriodMonths"),
            premium_period_years=c.get("PremiumPeriodYears"),
            premium_to_age=c.get("PremiumToAge"),
            is_rider=bool(c.get("RiderIndicator", False)),
            is_guaranteed=bool(c.get("GuaranteedIndicator", True)),
            waiting_period_months=c.get("WaitingPeriodMonths"),
            rate_class=c.get("RateClass"),
        )
        for c in content.get("Coverage", [])
    ]
    parties = [
        models.RoleAssignment(
            party_id=role["PartyOccurrenceID"],
            relationship=models.Relationship(role["RelationshipCode"]),
            share_percent=role.get("SharePercent"),
            is_primary=bool(role.get("PrimaryIndicator", False)),
        )
        for role in content.get("PartyRole", [])
    ]
    events = [
        models.LifeEvent(
            event_id=event["EventID"],
            event_type=event["EventTypeCode"],
            effective_date=date.fromisoformat(str(event["EffectiveDate"])[:10]),
            status=models.PolicyStatus(event.get("EventStatusCode", "active")),
            amount=event.get("EventAmount"),
        )
        for event in content.get("LifeEvent", [])
    ]
    return models.Policy(
        policy_id=content["PolicyNumber"],
        product_id=content.get("ProductID", content["ProductCode"]),
        product_code=content["ProductCode"],
        status=models.PolicyStatus(content.get("PolicyStatusCode", "quoted")),
        issue_date=_as_date(content.get("IssueDate")),
        effective_date=_as_date(content.get("EffectiveDate")),
        expiration_date=_as_date(content.get("ExpirationDate")),
        face_amount=content.get("FaceAmount", 0.0),
        currency=content.get("CurrencyCode", "USD"),
        payment_mode=models.PaymentMode(content.get("PaymentModeCode", "monthly")),
        annualised_premium=content.get("AnnualisedPremiumAmount", 0.0),
        state_of_issue=content["StateCode"],
        lob=content.get("LineOfBusiness", "Life"),
        master_group=content.get("MasterGroupIndicator"),
        coverages=coverages,
        parties=parties,
        events=events,
    )


def _as_date(value: Any) -> date | None:  # noqa: ANN401
    if not value:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
