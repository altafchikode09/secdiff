"""The curated 100-concept XBRL catalogue for 10-K analysis.

This is the single source of truth for which us-gaap concepts secdiff
tracks (see ``xbrl.compute_delta``). Every concept is a real us-gaap (or,
for share counts, dei) tag name as filers actually use it, with an alias
list in priority order: companies tag the same line item differently
(Apple sells through ``CostOfGoodsAndServicesSold``, Walmart through
``CostOfRevenue``), so aliases are how one catalogue covers many filers.

Matching discipline (enforced in xbrl.py):
- A concept is only delta'd when the SAME alias is reported by BOTH
  filings being compared; values are matched per filing by accession
  number, so each column is exactly what that filing reported.
- Aliases are scored: an alias reported by both filings beats an alias
  reported by only one, regardless of priority order. A concept found in
  only one filing is listed as retagged/unmapped, never silently merged.
- ``kind``: "flow" = duration of roughly one fiscal year (income
  statement / cash flow); "stock" = instant at the period end (balance
  sheet). ``unit``: the expected unit family ("money", "shares",
  "per-share", "pure") used to pick the right unit series.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Concept:
    aliases: tuple[str, ...]     # us-gaap tag candidates, priority order
    label: str                   # human-readable line-item label
    category: str                # Income statement / Balance sheet / Cash flow / Per-share & other
    kind: str                    # "flow" | "stock"
    unit: str                    # "money" | "shares" | "per-share" | "pure"
    taxonomy: str = "us-gaap"    # taxonomy the aliases live in


def _c(aliases, label, category, kind, unit, taxonomy="us-gaap"):
    return Concept(tuple(aliases), label, category, kind, unit, taxonomy)


IS = "Income statement"
BS = "Balance sheet"
CF = "Cash flow"
PS = "Per-share & other"

CONCEPTS: list[Concept] = [
    # ---- Income statement (1-27) ----
    _c(["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
        "SalesRevenueNet", "TotalRevenues"], "Revenue", IS, "flow", "money"),  # 1
    _c(["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"],
       "Cost of revenue", IS, "flow", "money"),  # 2
    _c(["GrossProfit"], "Gross profit", IS, "flow", "money"),  # 3
    _c(["ResearchAndDevelopmentExpense"], "R&D expense", IS, "flow", "money"),  # 4
    _c(["SellingGeneralAndAdministrativeExpense"], "SG&A expense", IS, "flow", "money"),  # 5
    _c(["SellingAndMarketingExpense"], "Selling & marketing expense", IS, "flow", "money"),  # 6
    _c(["GeneralAndAdministrativeExpense"], "General & administrative expense", IS, "flow", "money"),  # 7
    _c(["OperatingIncomeLoss"], "Operating income", IS, "flow", "money"),  # 8
    _c(["CostsAndExpenses"], "Total costs & expenses", IS, "flow", "money"),  # 9
    _c(["InterestExpense", "InterestExpenseDebt", "InterestExpenseNonoperating",
        "InterestAndDebtExpense"], "Interest expense", IS, "flow", "money"),  # 10
    _c(["InvestmentIncomeNet", "InterestAndDividendIncomeOperating",
        "InterestIncomeExpenseNet", "InvestmentIncomeExpense"],
       "Interest & investment income", IS, "flow", "money"),  # 11
    _c(["NonoperatingIncomeExpense", "OtherNonoperatingIncomeExpense"],
       "Other nonoperating income (expense)", IS, "flow", "money"),  # 12
    _c(["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
       "Income before income taxes", IS, "flow", "money"),  # 13
    _c(["IncomeTaxExpenseBenefit"], "Income tax expense", IS, "flow", "money"),  # 14
    _c(["IncomeLossFromContinuingOperations",
        "IncomeLossFromContinuingOperationsIncludingPortionAttributableToNoncontrollingInterest"],
       "Income from continuing operations", IS, "flow", "money"),  # 15
    _c(["NetIncomeLoss", "ProfitLoss"], "Net income", IS, "flow", "money"),  # 16
    _c(["OtherComprehensiveIncomeLossNetOfTax"], "Other comprehensive income", IS, "flow", "money"),  # 17
    _c(["ComprehensiveIncomeNetOfTax"], "Comprehensive income", IS, "flow", "money"),  # 18
    _c(["EffectiveIncomeTaxRateContinuingOperations"], "Effective tax rate", IS, "flow", "pure"),  # 19
    _c(["EquityInEarningsOfUnconsolidatedSubsidiaries"],
       "Equity in earnings of affiliates", IS, "flow", "money"),  # 20
    _c(["AssetImpairmentCharges", "ImpairmentOfLongLivedAssetsHeldForUse",
        "ImpairmentOfIntangibleAssetsExcludingGoodwill"], "Impairment charges", IS, "flow", "money"),  # 21
    _c(["GoodwillImpairmentLoss"], "Goodwill impairment", IS, "flow", "money"),  # 22
    _c(["RestructuringCharges", "RestructuringCharge"], "Restructuring charges", IS, "flow", "money"),  # 23
    _c(["OperatingLeaseCost"], "Operating lease cost", IS, "flow", "money"),  # 24
    _c(["Depreciation"], "Depreciation expense", IS, "flow", "money"),  # 25
    _c(["AmortizationOfIntangibleAssets"], "Amortisation of intangible assets", IS, "flow", "money"),  # 26
    _c(["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
       "Stock-based compensation (P&L)", IS, "flow", "money"),  # 27

    # ---- Balance sheet (28-73) ----
    _c(["CashAndCashEquivalentsAtCarryingValue"], "Cash & equivalents", BS, "stock", "money"),  # 28
    _c(["MarketableSecuritiesCurrent", "ShortTermInvestments",
        "AvailableForSaleSecuritiesDebtSecuritiesCurrent"], "Short-term investments", BS, "stock", "money"),  # 29
    _c(["AccountsReceivableNetCurrent", "AccountsReceivableNet"],
       "Accounts receivable, net", BS, "stock", "money"),  # 30
    _c(["InventoryNet", "InventoryFinishedGoodsNet",
        "InventoryCrudeOilProductsAndMerchandise", "EnergyRelatedInventory"],
       "Inventory", BS, "stock", "money"),  # 31
    _c(["OtherAssetsCurrent", "PrepaidExpenseAndOtherAssetsCurrent", "PrepaidExpenseCurrent"],
       "Other current assets", BS, "stock", "money"),  # 32
    _c(["AssetsCurrent"], "Total current assets", BS, "stock", "money"),  # 33
    _c(["PropertyPlantAndEquipmentGross"], "PP&E, gross", BS, "stock", "money"),  # 34
    _c(["AccumulatedDepreciationDepletionAndAmortizationPropertyPlantAndEquipment"],
       "Accumulated depreciation", BS, "stock", "money"),  # 35
    _c(["PropertyPlantAndEquipmentNet"], "PP&E, net", BS, "stock", "money"),  # 36
    _c(["OperatingLeaseRightOfUseAsset"], "Operating lease ROU assets", BS, "stock", "money"),  # 37
    _c(["FinanceLeaseRightOfUseAsset"], "Finance lease ROU assets", BS, "stock", "money"),  # 38
    _c(["Goodwill"], "Goodwill", BS, "stock", "money"),  # 39
    _c(["FiniteLivedIntangibleAssetsNet", "IntangibleAssetsNetExcludingGoodwill",
        "IntangibleAssetsNet"], "Intangible assets, net", BS, "stock", "money"),  # 40
    _c(["LongTermInvestments", "MarketableSecuritiesNoncurrent",
        "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent"], "Long-term investments", BS, "stock", "money"),  # 41
    _c(["DeferredTaxAssetsNet", "DeferredIncomeTaxAssetsNet"], "Deferred tax assets, net", BS, "stock", "money"),  # 42
    _c(["OtherAssetsNoncurrent"], "Other noncurrent assets", BS, "stock", "money"),  # 43
    _c(["Assets"], "Total assets", BS, "stock", "money"),  # 44
    _c(["AccountsPayableCurrent", "AccountsPayable"], "Accounts payable", BS, "stock", "money"),  # 45
    _c(["AccruedLiabilitiesCurrent", "OtherAccruedLiabilitiesCurrent"],
       "Accrued liabilities", BS, "stock", "money"),  # 46
    _c(["AccruedIncomeTaxesCurrent"], "Accrued income taxes (current)", BS, "stock", "money"),  # 47
    _c(["LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings",
        "LongTermDebtAndCapitalLeaseObligationsCurrent"], "Current debt", BS, "stock", "money"),  # 48
    _c(["ContractWithCustomerLiabilityCurrent", "DeferredRevenueCurrent"],
       "Deferred revenue (current)", BS, "stock", "money"),  # 49
    _c(["OperatingLeaseLiabilityCurrent"], "Operating lease liability (current)", BS, "stock", "money"),  # 50
    _c(["FinanceLeaseLiabilityCurrent"], "Finance lease liability (current)", BS, "stock", "money"),  # 51
    _c(["LiabilitiesCurrent"], "Total current liabilities", BS, "stock", "money"),  # 52
    _c(["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"],
       "Long-term debt (noncurrent)", BS, "stock", "money"),  # 53
    _c(["LiabilitiesNoncurrent"], "Total noncurrent liabilities", BS, "stock", "money"),  # 54
    _c(["DeferredIncomeTaxLiabilitiesNet", "DeferredIncomeTaxLiabilities",
        "DeferredTaxLiabilities"], "Deferred tax liabilities", BS, "stock", "money"),  # 55
    _c(["OperatingLeaseLiabilityNoncurrent"], "Operating lease liability (noncurrent)", BS, "stock", "money"),  # 56
    _c(["FinanceLeaseLiabilityNoncurrent"], "Finance lease liability (noncurrent)", BS, "stock", "money"),  # 57
    _c(["PensionAndOtherPostretirementDefinedBenefitPlansLiabilitiesNoncurrent",
        "PensionAndOtherPostretirementBenefitPlansLiabilitiesNoncurrent",
        "DefinedBenefitPlanLiabilitiesNoncurrent"],
       "Pension & postretirement liabilities", BS, "stock", "money"),  # 58
    _c(["UnrecognizedTaxBenefits"], "Unrecognized tax benefits", BS, "stock", "money"),  # 59
    _c(["OtherLiabilitiesNoncurrent"], "Other noncurrent liabilities", BS, "stock", "money"),  # 60
    _c(["Liabilities"], "Total liabilities", BS, "stock", "money"),  # 61
    _c(["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
       "Total equity", BS, "stock", "money"),  # 62
    _c(["RetainedEarningsAccumulatedDeficit"], "Retained earnings", BS, "stock", "money"),  # 63
    _c(["AccumulatedOtherComprehensiveIncomeLossNetOfTax"], "Accumulated other comprehensive income", BS, "stock", "money"),  # 64
    _c(["AdditionalPaidInCapital", "AdditionalPaidInCapitalCommonStock"],
       "Additional paid-in capital", BS, "stock", "money"),  # 65
    _c(["CommonStockValue"], "Common stock value", BS, "stock", "money"),  # 66
    _c(["TreasuryStockValue", "TreasuryStockValueAcquiredCostMethod", "TreasuryStockCommonValue"],
       "Treasury stock value", BS, "stock", "money"),  # 67
    _c(["MinorityInterest"], "Noncontrolling interests", BS, "stock", "money"),  # 68
    _c(["LiabilitiesAndStockholdersEquity"], "Total liabilities & equity", BS, "stock", "money"),  # 69
    _c(["CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding"],
       "Shares outstanding", BS, "stock", "shares"),  # 70
    _c(["CommonStockSharesIssued"], "Shares issued", BS, "stock", "shares"),  # 71
    _c(["CommonStockSharesAuthorized"], "Shares authorised", BS, "stock", "shares"),  # 72
    _c(["AllowanceForDoubtfulAccountsReceivableCurrent", "AllowanceForCreditLossesCurrent"],
       "Allowance for doubtful accounts", BS, "stock", "money"),  # 73

    # ---- Cash flow (74-95) ----
    _c(["NetCashProvidedByUsedInOperatingActivities"], "Operating cash flow", CF, "flow", "money"),  # 74
    _c(["NetCashProvidedByUsedInInvestingActivities"], "Investing cash flow", CF, "flow", "money"),  # 75
    _c(["NetCashProvidedByUsedInFinancingActivities"], "Financing cash flow", CF, "flow", "money"),  # 76
    _c(["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
       "Capital expenditures", CF, "flow", "money"),  # 77
    _c(["DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
        "DepreciationAmortizationAndAccretionNet"], "Depreciation, depletion & amortisation", CF, "flow", "money"),  # 78
    _c(["DeferredIncomeTaxExpenseBenefit"], "Deferred income taxes (cash-flow stmt)", CF, "flow", "money"),  # 79
    _c(["IncreaseDecreaseInInventories", "IncreaseDecreaseInRetailRelatedInventories"],
       "Change in inventories (CF)", CF, "flow", "money"),  # 80
    _c(["IncreaseDecreaseInAccountsReceivable", "IncreaseDecreaseInAccountsAndNotesReceivable",
        "IncreaseDecreaseInAccountsAndOtherReceivables"], "Change in receivables (CF)", CF, "flow", "money"),  # 81
    _c(["IncreaseDecreaseInAccountsPayable"], "Change in payables (CF)", CF, "flow", "money"),  # 82
    _c(["PaymentsOfDividendsCommonStock", "PaymentsOfDividends", "DividendsCommonStockCash",
        "PaymentsOfCashDividends"], "Dividends paid", CF, "flow", "money"),  # 83
    _c(["PaymentsForRepurchaseOfCommonStock"], "Share repurchases", CF, "flow", "money"),  # 84
    _c(["ProceedsFromIssuanceOfLongTermDebt", "ProceedsFromDebtMaturingInMoreThanThreeMonths",
        "ProceedsFromIssuanceOfDebt"], "Proceeds from long-term debt", CF, "flow", "money"),  # 85
    _c(["RepaymentsOfLongTermDebt", "RepaymentsOfDebtMaturingInMoreThanThreeMonths", "RepaymentsOfDebt"],
       "Repayments of long-term debt", CF, "flow", "money"),  # 86
    _c(["ProceedsFromIssuanceOfCommonStock", "ProceedsFromIssuanceOrSaleOfEquity"],
       "Proceeds from issuing stock", CF, "flow", "money"),  # 87
    _c(["PaymentsToAcquireBusinessesNetOfCashAcquired", "PaymentsToAcquireBusinessNetOfCashAcquired"],
       "Acquisitions, net of cash acquired", CF, "flow", "money"),  # 88
    _c(["ProceedsFromMaturitiesPrepaymentsAndCallsOfAvailableForSaleSecurities",
        "ProceedsFromSaleOfAvailableForSaleSecuritiesDebt",
        "ProceedsFromSaleAndMaturityOfAvailableForSaleSecurities"],
       "Proceeds from maturities/sales of investments", CF, "flow", "money"),  # 89
    _c(["PaymentsToAcquireAvailableForSaleSecuritiesDebt", "PaymentsToAcquireInvestments",
        "PaymentsToAcquireMarketableSecurities", "PaymentsToAcquireAvailableForSaleSecurities"],
       "Purchases of investments", CF, "flow", "money"),  # 90
    _c(["ProceedsFromSaleOfPropertyPlantAndEquipment"], "Proceeds from sale of PP&E", CF, "flow", "money"),  # 91
    _c(["InterestPaidNet", "InterestPaid"], "Interest paid (supplemental)", CF, "flow", "money"),  # 92
    _c(["IncomeTaxesPaidNet", "IncomeTaxesPaid"], "Income taxes paid", CF, "flow", "money"),  # 93
    _c(["CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
        "CashAndCashEquivalentsPeriodIncreaseDecrease"], "Net change in cash (incl. restricted)", CF, "flow", "money"),  # 94
    # (end-of-period cash is tagged as INSTANT facts — start=None — even
    # though it sits on the cash-flow statement, hence kind="stock".)
    _c(["CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
       "Cash & restricted cash, end of period", CF, "stock", "money"),  # 95

    # ---- Per-share & other (96-100) ----
    _c(["EarningsPerShareBasic"], "EPS (basic)", PS, "flow", "per-share"),  # 96
    _c(["EarningsPerShareDiluted"], "EPS (diluted)", PS, "flow", "per-share"),  # 97
    _c(["WeightedAverageNumberOfSharesOutstandingBasic"], "Weighted avg shares (basic)", PS, "flow", "shares"),  # 98
    _c(["WeightedAverageNumberOfDilutedSharesOutstanding"], "Weighted avg shares (diluted)", PS, "flow", "shares"),  # 99
    _c(["CommonStockDividendsPerShareDeclared", "CommonStockDividendsPerShareCashPaid"],
       "Dividends per share declared", PS, "flow", "per-share"),  # 100
]

# Headline ("large") concepts: a miss on any of these for a filer that
# presents the line item is a serious bug, not a niche gap. Used by the
# coverage diagnostics to classify misses.
LARGE_LABELS = {
    "Revenue", "Cost of revenue", "Gross profit", "Operating income",
    "Income before income taxes", "Income tax expense", "Net income",
    "EPS (basic)", "EPS (diluted)",
    "Cash & equivalents", "Total current assets", "Total assets",
    "Total current liabilities", "Total liabilities", "Total equity",
    "Total liabilities & equity",
    "Operating cash flow", "Investing cash flow", "Financing cash flow",
    "Capital expenditures", "Dividends paid", "Share repurchases",
}
