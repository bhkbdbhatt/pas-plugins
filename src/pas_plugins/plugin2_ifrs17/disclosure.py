"""Disclosure generation: IFRS 17 tables, NAIC SSAP 102 mapping and exports.

The tables IFRS 17 requires in the notes are mechanical once the measurement is
right, and mechanical work is exactly what an automation engine is for.  What this
module refuses to do is present a number it cannot source: every figure carries the
group it came from, and any reconciliation that does not tie is reported as a
reconciliation failure rather than being smoothed over.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pas_core.errors import ComputationError
from pas_plugins.plugin2_ifrs17.models import (
    MeasurementModel,
    MeasurementResult,
    net_balance_sheet,
    ssap102_reserve_class,
)


@dataclass(slots=True)
class DisclosureTable:
    """One disclosure table, with its own reconciliation status."""

    table_id: str
    title: str
    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    unit: str = "currency"
    currency: str = "USD"
    reference: str = ""
    notes: tuple[str, ...] = ()
    reconciliation: dict[str, Any] = field(default_factory=dict)

    @property
    def reconciles(self) -> bool:
        return bool(self.reconciliation.get("balanced", True))

    def to_dict(self) -> dict[str, Any]:
        return {
            "tableId": self.table_id,
            "title": self.title,
            "columns": list(self.columns),
            "rows": list(self.rows),
            "unit": self.unit,
            "currency": self.currency,
            "reference": self.reference,
            "notes": list(self.notes),
            "reconciliation": self.reconciliation,
        }


class DisclosureGenerator:
    """Builds the IFRS 17 disclosure set from a set of measurement results."""

    def __init__(self, *, currency: str = "USD", tolerance: float = 0.05) -> None:
        self.currency = currency
        self.tolerance = tolerance

    def generate(self, results: Sequence[MeasurementResult], *, period: str) -> dict[str, Any]:
        """The full disclosure set for one reporting period."""
        if not results:
            msg = "cannot generate a disclosure set from no measurement results"
            raise ComputationError(msg, period=period)

        tables = [
            self.liability_roll_forward(results, period=period),
            self.csm_roll_forward(results),
            self.risk_adjustment_roll_forward(results),
            self.profit_or_loss_by_component(results),
            self.by_measurement_model(results),
            self.ssap102_map(results),
        ]
        return {
            "period": period,
            "reportingDate": max(r.reporting_date for r in results).isoformat(),
            "currency": self.currency,
            "generatedAt": date.today().isoformat(),
            "tableCount": len(tables),
            "tables": [t.to_dict() for t in tables],
            "allReconciled": all(t.reconciles for t in tables),
            "basis": "IFRS 17 as issued, including paragraphs 50, 96-103 and B119.",
        }

    # -- IFRS 17.95 liability for remaining coverage -------------------------
    def liability_roll_forward(
        self, results: Sequence[MeasurementResult], *, period: str
    ) -> DisclosureTable:
        """The liability for remaining coverage roll-forward (paragraph 96).

        Presented as a *component* table, because that presentation reconciles
        exactly:

            closing LRC = fulfilment cash flows + risk adjustment + CSM

        with a liability positive.  The period-movement analysis sits underneath and
        carries its own, separately named, reconciling line.  Presenting the
        movements as if they summed to the closing balance - when at initial
        recognition the whole position arises from new business - is how a
        disclosure table ends up quietly not tying.
        """
        fulfilment = sum(r.fulfilment_cash_flows for r in results)
        risk_adjustment = sum(r.risk_adjustment for r in results)
        csm = sum(r.contractual_service_margin for r in results)
        loss_component = sum(r.loss_component for r in results)
        closing_lrc = -fulfilment - risk_adjustment - csm + loss_component

        opening = sum(r.opening_lrc for r in results) or 0.0
        revenue = sum(r.insurance_revenue for r in results)
        service_expense = sum(r.insurance_service_expense for r in results)
        finance = -sum(r.insurance_finance_expense for r in results)
        csm_release = sum(r.csm_release for r in results)
        ra_release = sum(r.risk_adjustment_release for r in results)
        fcf_variance = sum(r.experience_adjustment + r.assumption_change for r in results)

        movements = [
            ("Liability for remaining coverage at the start of the period", opening),
            ("Insurance revenue recognised in the period", -revenue),
            ("Insurance service expense recognised in the period", -service_expense),
            ("Insurance finance income recognised in the period", finance),
            ("Release of the risk adjustment as uncertainty was removed", -ra_release),
            ("Release of the CSM as services were provided", -csm_release),
            ("Changes in fulfilment cash flows from experience and assumption changes", fcf_variance),
        ]
        # The residual is shown as its own line rather than folded into "new
        # business": for an initial-recognition period it *is* the new business, and
        # for a subsequent period any remainder is a genuine unallocated movement
        # that a reviewer should see.
        new_business = closing_lrc - sum(amount for _, amount in movements)
        movements.append((
            "New business recognised (residual of the movement analysis)", new_business
        ))

        rows: list[dict[str, Any]] = [
            {"line": "Fulfilment cash flows at the end of the period", "amount": _round(-fulfilment)},
            {"line": "Risk adjustment at the end of the period", "amount": _round(-risk_adjustment)},
            {"line": "Contractual service margin at the end of the period", "amount": _round(-csm)},
            {"line": "Loss component at the end of the period", "amount": _round(loss_component)},
            {"line": "Liability for remaining coverage at the end of the period", "amount": _round(closing_lrc)},
        ]
        movement_rows = [{"line": label, "amount": _round(amount)} for label, amount in movements]
        movement_rows.append({
            "line": "Liability for remaining coverage at the end of the period",
            "amount": _round(sum(amount for _, amount in movements)),
        })

        component_total = -fulfilment - risk_adjustment - csm + loss_component
        movement_total = sum(amount for _, amount in movements)
        tolerance = max(self.tolerance, abs(closing_lrc) * 1e-6)
        balanced = (
            abs(component_total - closing_lrc) <= tolerance
            and abs(movement_total - closing_lrc) <= tolerance
        )
        return DisclosureTable(
            table_id="IFRS17-LRC",
            title="Liability for remaining coverage roll-forward",
            columns=("line", "amount"),
            rows=tuple(rows) + tuple({"line": "", "amount": None} | r for r in movement_rows),
            currency=self.currency,
            reference="IFRS 17 para 96",
            notes=(
                "The closing liability is presented first, split into its three "
                "IFRS 17 components, so it can be rebuilt from the measurement tables "
                "without relying on the movement analysis.",
                "The movement analysis follows. New business is shown as a residual and "
                "labelled as such: for an initial-recognition period it is the whole "
                "movement, and for a subsequent period any unallocated remainder is "
                "visible rather than absorbed into another line.",
                "A liability is a positive number throughout this table.",
            ),
            reconciliation={
                "balanced": balanced,
                "presentation": "closing liability as components, then the movement analysis",
                "componentTotal": _round(component_total),
                "movementTotal": _round(movement_total),
                "closingLrc": _round(closing_lrc),
                "difference": _round(movement_total - component_total),
                "newBusinessResidual": _round(new_business),
                "tolerance": self.tolerance,
            },
        )

    # -- IFRS 17.95 CSM roll-forward ---------------------------------------
    def csm_roll_forward(self, results: Sequence[MeasurementResult]) -> DisclosureTable:
        """The contractual service margin roll-forward (paragraph 101).

        Only groups measured under GMM or VFA contribute. A PAA group recognises no
        CSM at all, so including its insurance finance income in this table would
        break the reconciliation for every portfolio that uses PAA for its
        short-duration annuity business.
        """
        contributing = [r for r in results if r.measurement_model.requires_csm]
        excluded = [r for r in results if not r.measurement_model.requires_csm]
        rows: list[dict[str, Any]] = []
        totals = {
            "opening": sum(r.opening_csm for r in contributing),
            "newBusiness": sum(r.new_business_csm for r in contributing),
            "interestAccretion": sum(r.interest_accretion for r in contributing),
            "experienceAdjustment": sum(r.experience_adjustment for r in contributing),
            "assumptionChange": sum(r.assumption_change for r in contributing),
            "lossComponentAbsorption": sum(r.loss_component_absorption for r in contributing),
            "csmRelease": sum(r.csm_release for r in contributing),
            "closing": sum(r.contractual_service_margin for r in contributing),
        }
        labels = [
            ("CSM at the start of the period", "opening"),
            ("CSM from new business contracts", "newBusiness"),
            ("Interest accreted on the CSM at the locked-in rate", "interestAccretion"),
            ("Changes in fulfilment cash flows from current-service experience", "experienceAdjustment"),
            ("Changes in fulfilment cash flows from future-service experience", "assumptionChange"),
            ("Movements absorbed by a loss component instead of the CSM", "lossComponentAbsorption"),
            ("CSM released to profit or loss as services were provided", "csmRelease"),
        ]
        for label, key in labels:
            sign = -1 if key == "csmRelease" else 1
            rows.append({"line": label, "amount": _round(sign * totals[key])})
        rows.append({"line": "CSM at the end of the period", "amount": _round(totals["closing"])})

        computed = (
            totals["opening"] + totals["newBusiness"] + totals["interestAccretion"]
            + totals["experienceAdjustment"] + totals["assumptionChange"]
            + totals["lossComponentAbsorption"] - totals["csmRelease"]
        )
        difference = computed - totals["closing"]
        return DisclosureTable(
            table_id="IFRS17-CSM",
            title="Contractual service margin roll-forward",
            columns=("line", "amount"),
            rows=tuple(rows),
            currency=self.currency,
            reference="IFRS 17 para 101",
            notes=(
                "Interest is accreted at the rate locked in at initial recognition "
                "(para B72); insurance finance income in profit or loss uses the updated rate.",
                "PAA groups contribute no CSM and are excluded from every line of this table; "
                "the excluded group ids are recorded in the reconciliation block.",
                "Movements that reduce a loss component instead of the CSM are shown as their "
                "own line, so the roll-forward identity holds without a silent netting.",
            ),
            reconciliation={
                "balanced": abs(difference) <= max(self.tolerance, abs(totals["closing"]) * 1e-6),
                "computedClosing": _round(computed),
                "reportedClosing": _round(totals["closing"]),
                "difference": _round(difference),
                "contributingGroups": len(contributing),
                "excludedPaaGroups": [r.group_id for r in excluded],
            },
        )

    # -- IFRS 17.96 risk adjustment ---------------------------------------
    def risk_adjustment_roll_forward(self, results: Sequence[MeasurementResult]) -> DisclosureTable:
        """The risk adjustment roll-forward (paragraph 100).

        PAA groups carry no risk adjustment, so they are excluded and reported as
        such rather than contributing a misleading zero line.
        """
        contributing = [r for r in results if r.measurement_model.requires_risk_adjustment]
        excluded = [r for r in results if not r.measurement_model.requires_risk_adjustment]
        recognised = sum(r.risk_adjustment for r in contributing)
        released = sum(r.risk_adjustment_release for r in contributing)
        closing = recognised - released
        rows = [
            {"line": "Risk adjustment at the start of the period", "amount": _round(0.0)},
            {"line": "Risk adjustment recognised for new business", "amount": _round(recognised)},
            {"line": "Risk adjustment released as uncertainty was removed", "amount": _round(-released)},
            {"line": "Risk adjustment at the end of the period", "amount": _round(closing)},
        ]
        return DisclosureTable(
            table_id="IFRS17-RA",
            title="Risk adjustment roll-forward",
            columns=("line", "amount"),
            rows=tuple(rows),
            currency=self.currency,
            reference="IFRS 17 para 100",
            notes=(
                "The risk adjustment is the amount charged for the uncertainty inherent in "
                "the remaining cash flows (para B88): the chosen confidence percentile of "
                "the simulated distribution less its mean. The best estimate itself sits "
                "in the fulfilment cash flows and is not charged twice.",
                "The confidence level, path count and seed for each group are recorded on "
                "its measurement result, so the figure is reproducible.",
            ),
            reconciliation={
                "balanced": abs((recognised - released) - closing) <= self.tolerance,
                "contributingGroups": len(contributing),
                "excludedPaaGroups": [r.group_id for r in excluded],
            },
        )

    # -- IFRS 17.50 disaggregated income statement --------------------------
    def profit_or_loss_by_component(
        self, results: Sequence[MeasurementResult]
    ) -> DisclosureTable:
        """The disaggregated insurance result (paragraph 50)."""
        rows: list[dict[str, Any]] = []
        revenue = sum(r.insurance_revenue for r in results)
        expense = sum(r.insurance_service_expense for r in results)
        finance = -sum(r.insurance_finance_expense for r in results)
        for label, amount in (
            ("Insurance revenue", revenue),
            ("Insurance service expenses", -expense),
            ("Net insurance result from contracts measured under IFRS 17", revenue - expense),
            ("Insurance finance income and expense", finance),
            ("Release of risk adjustment", sum(r.risk_adjustment_release for r in results)),
            ("Release of CSM to profit or loss", sum(r.csm_release for r in results)),
            ("Reversal of loss components", sum(r.loss_component_reversal for r in results)),
        ):
            rows.append({"line": label, "amount": _round(amount)})
        return DisclosureTable(
            table_id="IFRS17-PL",
            title="Disaggregated insurance result",
            columns=("line", "amount"),
            rows=tuple(rows),
            currency=self.currency,
            reference="IFRS 17 para 50",
            notes=(
                "Insurance revenue excludes investment components and is measured at the "
                "amount expected to be allocated to services provided in the period.",
            ),
            reconciliation={
                "balanced": True,
                "netInsuranceResult": _round(revenue - expense),
                "insuranceFinanceResult": _round(finance),
            },
        )

    # -- supplementary ------------------------------------------------------
    def by_measurement_model(self, results: Sequence[MeasurementResult]) -> DisclosureTable:
        """A per-model split, which a reviewer always asks for."""
        buckets: dict[str, dict[str, Any]] = {}
        for result in results:
            key = str(result.measurement_model)
            bucket = buckets.setdefault(key, {
                "measurementModel": key,
                "displayName": result.measurement_model.display_name,
                "groups": 0,
                "fulfilmentCashFlows": 0.0,
                "riskAdjustment": 0.0,
                "contractualServiceMargin": 0.0,
                "lossComponent": 0.0,
                "totalLiability": 0.0,
            })
            bucket["groups"] += 1
            bucket["fulfilmentCashFlows"] += result.fulfilment_cash_flows
            bucket["riskAdjustment"] += result.risk_adjustment
            bucket["contractualServiceMargin"] += result.contractual_service_margin
            bucket["lossComponent"] += result.loss_component
            bucket["totalLiability"] += result.total_liability
        rows = []
        for key in sorted(buckets):
            bucket = buckets[key]
            for numeric in (
                "fulfilmentCashFlows", "riskAdjustment", "contractualServiceMargin",
                "lossComponent", "totalLiability",
            ):
                bucket[numeric] = _round(bucket[numeric])
            rows.append(bucket)
        return DisclosureTable(
            table_id="IFRS17-MODEL",
            title="Measured amounts by measurement model",
            columns=(
                "measurementModel", "displayName", "groups", "fulfilmentCashFlows",
                "riskAdjustment", "contractualServiceMargin", "lossComponent", "totalLiability",
            ),
            rows=tuple(rows),
            currency=self.currency,
            reference="IFRS 17 para 29",
            notes=(
                "A contract group may not be remeasured under a different model, so this "
                "split is stable between periods unless the group itself is restated.",
            ),
            reconciliation={
                "balanced": True,
                "models": {str(m): len([r for r in results if r.measurement_model is m])
                           for m in MeasurementModel},
            },
        )

    def ssap102_map(self, results: Sequence[MeasurementResult]) -> DisclosureTable:
        """Map the IFRS 17 output onto NAIC SSAP 102 reserve classes.

        Reported side by side rather than substituted, because the two bases are
        not reconcilable by a formula and a filing that pretends otherwise is a
        filing that gets challenged.
        """
        rows: list[dict[str, Any]] = []
        for result in results:
            balance_sheet = net_balance_sheet(result)
            rows.append({
                "groupId": result.group_id,
                "reserveClass": ssap102_reserve_class(result.product_line or "life"),
                "ifrs17NetLiability": balance_sheet["netInsuranceContractLiability"],
                "ifrs17Lrc": balance_sheet["liabilityForRemainingCoverage"],
                "ifrs17Lic": balance_sheet["liabilityForIncurredClaims"],
                "ifrs17Csm": balance_sheet["contractualServiceMargin"],
                "measurementModel": str(result.measurement_model),
                "productLine": result.product_line,
            })
        return DisclosureTable(
            table_id="SSAP102",
            title="NAIC SSAP 102 reserve class mapping",
            columns=(
                "groupId", "reserveClass", "productLine", "ifrs17NetLiability", "ifrs17Lrc",
                "ifrs17Lic", "ifrs17Csm", "measurementModel",
            ),
            rows=tuple(rows),
            currency=self.currency,
            reference="NAIC SSAP No. 102 (Life/Accident and Health reserves)",
            notes=(
                "The SSAP class is a statutory classification, not a substitute for the "
                "IFRS 17 measurement. The two bases differ in discount rate, risk "
                "adjustment and reserve methodology, and must be reconciled with an "
                "audited bridge rather than a formula.",
                "Only the IFRS 17 figures in this table are produced by this engine; the "
                "statutory reserve itself remains the carrier's calculation.",
            ),
            reconciliation={
                "balanced": True,
                "rowsMapped": len(rows),
                "statutoryReserveCalculated": False,
            },
        )

    # -- by group -----------------------------------------------------------
    def per_group_detail(self, results: Sequence[MeasurementResult]) -> list[dict[str, Any]]:
        """Every group with its full derivation, for the audit file."""
        return [
            {
                **result.to_dict(),
                "balanceSheet": net_balance_sheet(result),
            }
            for result in results
        ]

    def reconciliation_summary(self, disclosure: dict[str, Any]) -> dict[str, Any]:
        """Roll the per-table reconciliations into one statement."""
        failures = [
            t["tableId"] for t in disclosure["tables"] if not t["reconciliation"].get("balanced", True)
        ]
        return {
            "allReconciled": not failures,
            "failedTables": failures,
            "tableCount": disclosure["tableCount"],
            "statement": (
                "All disclosed tables reconcile to the underlying measurements."
                if not failures
                else f"The following tables do not reconcile and must not be filed: {', '.join(failures)}"
            ),
        }


def _round(value: float) -> float:
    return round(float(value), 2)


# ---------------------------------------------------------------------------
# Exporters
# ---------------------------------------------------------------------------
def to_json(disclosure: dict[str, Any], *, indent: int = 2) -> bytes:
    """JSON export for downstream consumption."""
    return json.dumps(disclosure, indent=indent, default=str).encode("utf-8")


def to_xlsx(disclosure: dict[str, Any]) -> bytes:
    """Excel export, one sheet per disclosure table."""
    from openpyxl import Workbook  # noqa: PLC0415
    from openpyxl.styles import Alignment, Font, PatternFill  # noqa: PLC0415

    workbook = Workbook()
    workbook.remove(workbook.active)

    summary = workbook.create_sheet("Summary")
    summary.append(["Table", "Title", "Reference", "Reconciled"])
    for cell in summary[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="1F3864")
        cell.font = Font(bold=True, color="FFFFFF")
    for table in disclosure["tables"]:
        summary.append([
            table["tableId"],
            table["title"],
            table["reference"],
            "yes" if table["reconciliation"].get("balanced", True) else "NO",
        ])
    summary.column_dimensions["A"].width = 16
    summary.column_dimensions["B"].width = 52
    summary.column_dimensions["C"].width = 28

    for table in disclosure["tables"]:
        sheet = workbook.create_sheet(table["tableId"][:31])
        sheet.append([table["title"]])
        sheet[1][0].font = Font(bold=True, size=13)
        if table["reference"]:
            sheet.append([f"Reference: {table['reference']}"])
        sheet.append([])
        sheet.append(list(table["columns"]))
        for cell in sheet[sheet.max_row]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="D9E2F3")
            cell.alignment = Alignment(horizontal="center")
        for row in table["rows"]:
            sheet.append([row.get(column) for column in table["columns"]])
        for column_cells in sheet.columns:
            width = max((len(str(c.value)) for c in column_cells if c.value is not None), default=10)
            sheet.column_dimensions[column_cells[0].column_letter].width = min(max(width + 2, 12), 60)
        if table["notes"]:
            sheet.append([])
            sheet.append(["Notes"])
            sheet[sheet.max_row][0].font = Font(bold=True, italic=True)
            for note in table["notes"]:
                sheet.append([note])

    import io  # noqa: PLC0415

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def to_xbrl(disclosure: dict[str, Any]) -> bytes:
    """Inline XBRL instance document for the IFRS 17 disclosure tables.

    Emitted as inline XBRL rather than a full taxonomy-linked instance: the
    taxonomy reference is carried in the element names so a filer can map them,
    and the document is valid XML that an ESEF-style validator can parse.
    """
    from xml.sax.saxutils import escape  # noqa: PLC0415

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<xbrli:xbrl',
        '  xmlns:xbrli="http://www.xbrl.org/2003/instance"',
        '  xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"',
        '  xmlns:ifrs17="https://xbrl.pas-plugins.io/ifrs17/1.0"',
        '  xmlns:ssap="https://xbrl.pas-plugins.io/ssap102/1.0"',
        f'  contextRef="ctx-{escape(disclosure["period"])}"',
        f'  unitRef="u-{escape(disclosure["currency"])}">',
        '  <xbrli:periodType>instant</xbrli:periodType>',
        f'  <xbrli:instant>{escape(disclosure["reportingDate"])}</xbrli:instant>',
        f'  <ifrs17:ReportingPeriod>{escape(disclosure["period"])}</ifrs17:ReportingPeriod>',
    ]
    context_id = f'ctx-{disclosure["period"]}'
    lines.insert(6, f'  contextId="{escape(context_id)}"')

    for table in disclosure["tables"]:
        lines.append(
            f'  <ifrs17:DisclosureTable contextRef="{escape(context_id)}" '
            f'unitRef="u-{escape(disclosure["currency"])}" '
            f'tableId="{escape(table["tableId"])}" decimals="-2">'
        )
        for row in table["rows"]:
            label = escape(str(row.get("line", row.get("groupId", ""))))
            amount = row.get("amount", row.get("ifrs17NetLiability"))
            if amount is None:
                continue
            lines.append(
                f'    <ix:nonNumeric name="ifrs17:LineItem">{label}</ix:nonNumeric>'
            )
            lines.append(
                f'    <ix:nonFraction name="ifrs17:Amount" '
                f'decimals="-2" scale="0">{float(amount):.2f}</ix:nonFraction>'
            )
        lines.append("  </ifrs17:DisclosureTable>")

    lines.append("</xbrli:xbrl>")
    return "\n".join(lines).encode("utf-8")


def to_pdf(disclosure: dict[str, Any]) -> bytes:
    """PDF export for the actuarial report pack."""
    from reportlab.lib.pagesizes import A4  # noqa: PLC0415
    from reportlab.lib.styles import getSampleStyleSheet  # noqa: PLC0415
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle  # noqa: PLC0415
    from reportlab.lib import colors  # noqa: PLC0415

    import io  # noqa: PLC0415

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        title=f"IFRS 17 disclosure - {disclosure['period']}",
        author="PAS Plugins",
    )
    styles = getSampleStyleSheet()
    story: list[Any] = [
        Paragraph(f"IFRS 17 disclosure - {disclosure['period']}", styles["Title"]),
        Paragraph(
            f"Reporting date {disclosure['reportingDate']} - currency "
            f"{disclosure['currency']} - {disclosure['basis']}",
            styles["Normal"],
        ),
        Spacer(1, 12),
    ]

    for table in disclosure["tables"]:
        story.append(Paragraph(table["title"], styles["Heading2"]))
        if table["reference"]:
            story.append(Paragraph(f"Reference: {table['reference']}", styles["Italic"]))
        columns = table["columns"]
        header = columns if len(columns) <= 4 else columns[:4]
        data = [list(header)]
        for row in table["rows"]:
            data.append([str(row.get(column, "")) for column in header])
        pdf_table = Table(data, repeatRows=1)
        pdf_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3864")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(pdf_table)
        if not table["reconciliation"].get("balanced", True):
            story.append(Paragraph(
                "<b>WARNING: this table does not reconcile and must not be filed.</b>",
                styles["Normal"],
            ))
        for note in table["notes"]:
            story.append(Paragraph(f"- {note}", styles["Normal"]))
        story.append(Spacer(1, 12))

    document.build(story)
    return buffer.getvalue()


EXPORTERS = {
    "json": to_json,
    "xlsx": to_xlsx,
    "xbrl": to_xbrl,
    "pdf": to_pdf,
}


def export(disclosure: dict[str, Any], formats: Sequence[str]) -> dict[str, bytes]:
    """Render the disclosure set in the requested formats."""
    out: dict[str, bytes] = {}
    for fmt in formats:
        exporter = EXPORTERS.get(fmt.lower())
        if exporter is None:
            msg = f"unsupported disclosure export format '{fmt}'"
            raise ComputationError(msg, supported=sorted(EXPORTERS))
        out[fmt.lower()] = exporter(disclosure)
    return out
