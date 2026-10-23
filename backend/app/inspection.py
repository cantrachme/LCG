"""Read-only LCW row parsing and deterministic document comparison.

Only selectors/routes observed in LCW_BROWSER_DISCOVERY.md may dispatch actions.
Optional unobserved detail fields remain not_verified; edit forms are never opened.
"""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlsplit

from .contracts import ContractExtraction


class InspectionError(Exception):
    """Fixed operator-facing message, never raw page/exception content."""


def normalized(value):
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def iso_date(value):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except (ValueError, AttributeError):
            pass
    return None


def expected_identity(source: ContractExtraction):
    identity = source.original.identity
    result = {}
    for name in ("child_name", "child_id", "family_name", "family_id", "center"):
        field = getattr(identity, name)
        if field.status != "extracted" or not field.value or len(field.value) > 200:
            raise InspectionError("Review the PDF child name, child ID, family name, family ID and center before inspecting.")
        result[name] = field.value.strip()
    if not all(re.fullmatch(r"\d{1,20}", result[key]) for key in ("child_id", "family_id")):
        raise InspectionError("The PDF record identifiers must be numeric. Review the source before inspecting.")
    if not re.match(r"[A-Za-z]", result["family_name"]):
        raise InspectionError("The verified alphabetical search does not support this family name. Use local manual review.")
    return result


def search_group(family_name):
    index = (ord(family_name[0].upper()) - ord('A')) // 2 * 2
    return f"{chr(65 + index)} - {chr(min(65 + index + 1, 90))}"


def child_links(links, expected):
    """Inspect actual link targets, never construct a navigation URL."""
    matches = []
    for link in links:
        parsed = urlsplit(link["href"])
        if parsed.path.split('/')[-1] == "cgchild.do" and parse_qs(parsed.query).get("child_id") == [expected["child_id"]]:
            matches.append(link)
    if len(matches) != 1:
        raise InspectionError("The requested child ID was not uniquely found in the search results. No record was selected.")
    if normalized(matches[0]["label"]) != normalized(expected["child_name"]):
        raise InspectionError("The child ID search result has a different name. Resolve the identity conflict locally.")
    return matches[0]


def verify_identity(text, first_name, last_name, expected):
    child = re.findall(r"Child/Student ID\s*:\s*(\d+)\b", text)
    family = re.findall(r"Family ID\s*:?\s*(\d+)\b", text)
    center = re.findall(r"Family Center\s*:\s*([^\n]+)", text)
    # Do not accept an ID merely because it appears elsewhere in the document.
    if (set(child) != {expected['child_id']} or set(family) != {expected['family_id']}
            or not center or any(normalized(c) != normalized(expected['center']) for c in center)
            or normalized(f"{first_name} {last_name}") != normalized(expected['child_name'])):
        raise InspectionError("The opened record does not match the PDF child, family and center identifiers. Inspection stopped.")
    return {key: expected[key] for key in ('child_name', 'child_id', 'family_id', 'center')}


def verify_family_header(text, expected):
    families = re.findall(r"Family ID\s*:?\s*(\d+)\b", text)
    centers = re.findall(r"Family Center\s*:\s*([^\n]+)", text)
    if set(families) != {expected['family_id']} or not centers or any(normalized(c) != normalized(expected['center']) for c in centers):
        raise InspectionError("The family context changed during navigation. Return to the correct record and retry inspection.")


def parse_contract_rows(rows):
    records = []
    for row in rows:
        ids = {m[1] for link in row['links'] if (m := re.fullmatch(r"javascript:editContract\((\d+)\);?", link['href']))}
        if not ids:
            continue
        dates = [(i, iso_date(cell)) for i, cell in enumerate(row['cells']) if iso_date(cell)]
        if len(ids) != 1 or len(dates) < 2 or dates[0][0] < 1:
            raise InspectionError("The existing contract table layout is not recognized. No contract was inferred.")
        records.append({'contract_id': next(iter(ids)), 'agency': row['cells'][dates[0][0] - 1].strip(),
                        'start_date': dates[0][1], 'end_date': dates[1][1],
                        'evidence': 'Third Party Contract List'})
    return records


def parse_pra_rows(rows):
    records = []
    for row in rows:
        dates = [(i, iso_date(cell)) for i, cell in enumerate(row['cells']) if iso_date(cell)]
        if len(dates) < 2:
            continue
        start_index = dates[0][0]
        if start_index < 1:
            continue
        classroom = row['cells'][start_index - 1].strip()
        # Recognized existing row: Classroom ~ Center, dates, named rate ($amount).
        if ' ~ ' not in classroom:
            continue
        rates = [c.strip() for c in row['cells'][dates[1][0] + 1:] if re.search(r"\(\$[\d,.]+\)", c)]
        records.append({'classroom': classroom.split(' ~ ', 1)[0], 'center': classroom.split(' ~ ', 1)[1],
                        'start_date': dates[0][1], 'end_date': dates[1][1],
                        'rate_description': rates[0] if len(rates) == 1 else None,
                        'evidence': 'Program/Room Assignment: active assignments'})
    return records


def choose_record(records, start, end, agency=None):
    if len(records) == 1:
        return records[0], 'single'
    exact = [record for record in records if start and end and record['start_date'] == str(start)
             and record['end_date'] == str(end) and (agency is None or normalized(record.get('agency')) == normalized(agency))]
    return (exact[0], 'exact') if len(exact) == 1 else (None, 'missing' if not records else 'ambiguous')


def equal_values(source, observed):
    if isinstance(source, (dict, list, bool)):
        return source == observed
    try:
        return Decimal(str(source)) == Decimal(str(observed))
    except InvalidOperation:
        return normalized(source) == normalized(observed)


def compare_fields(source: ContractExtraction, observed):
    """Keep PDF provenance, portal evidence and future overrides separate."""
    rows = []
    original = source.model_dump(mode='json')['original']
    supported = ('identity', 'authorization', 'schedule', 'part_time', 'full_time', 'billing', 'existing_pra')
    for section in supported:
        for name, field in original[section].items():
            key = f'{section}.{name}'
            observation = observed.get(key, {'status': 'not_verified', 'value': None,
                                             'evidence': 'Not exposed by the verified read-only screens.'})
            if field['status'] != 'extracted' or field['value'] is None:
                status = 'not_verified'
                reason = 'The PDF value is missing or needs review.'
            elif observation['status'] == 'missing':
                status, reason = 'missing', 'The relevant portal record/value was confirmed absent.'
            elif observation['status'] != 'observed':
                status, reason = 'not_verified', observation['evidence']
            else:
                status = 'matching' if equal_values(field['value'], observation['value']) else 'different'
                reason = 'Compared with an observed portal value.'
            rows.append({'field': key, 'status': status, 'document': field,
                         'portal': observation, 'approved_change': None, 'reason': reason})
    return rows


def build_result(source, identity, contracts, assignments, contracts_verified=True, assignments_verified=True):
    auth, pra = source.original.authorization, source.original.existing_pra
    contract, c_reason = choose_record(contracts, auth.start_date.value, auth.end_date.value, source.original.identity.agency.value)
    assignment, p_reason = choose_record(assignments, pra.start_date.value, pra.end_date.value)
    if not contracts and not contracts_verified:
        c_reason = 'not_verified'
    if not assignments and not assignments_verified:
        p_reason = 'not_verified'
    observed = {f'identity.{key}': {'status': 'observed', 'value': value, 'evidence': 'Child detail identity fields'}
                for key, value in identity.items()}
    def put(section, field, value, evidence):
        observed[f'{section}.{field}'] = {'status': 'observed', 'value': value, 'evidence': evidence}
    if contract:
        for key in ('start_date', 'end_date'):
            put('authorization', key, contract[key], contract['evidence'])
        put('authorization', 'lcw_contract_number', contract['contract_id'], contract['evidence'])
        put('identity', 'agency', contract['agency'], contract['evidence'])
    elif not contracts and contracts_verified:
        for key in ('start_date', 'end_date', 'lcw_contract_number'):
            observed[f'authorization.{key}'] = {'status': 'missing', 'value': None, 'evidence': 'Observed empty contract list'}
        observed['identity.agency'] = {'status': 'missing', 'value': None, 'evidence': 'Observed empty contract list'}
    if assignment:
        for key in ('start_date', 'end_date'):
            put('existing_pra', key, assignment[key], assignment['evidence'])
        put('schedule', 'classroom', assignment['classroom'], assignment['evidence'])
    elif not assignments and assignments_verified:
        for key in ('start_date', 'end_date'):
            observed[f'existing_pra.{key}'] = {'status': 'missing', 'value': None, 'evidence': 'Observed empty active assignment list'}
    coverage = {'status': 'not_verified', 'reason': 'An unambiguous PRA and valid PDF contract dates are required.'}
    if assignment and auth.start_date.status == auth.end_date.status == 'extracted' and auth.start_date.value and auth.end_date.value:
        covered = assignment['start_date'] <= auth.start_date.value.isoformat() <= auth.end_date.value.isoformat() <= assignment['end_date']
        coverage = {'status': 'matching' if covered else 'different',
                    'reason': 'Existing PRA covers the source contract dates.' if covered else 'Contract dates extend outside the observed PRA period.'}
    notes = ['Inspection is read-only. Existing records require review before any future modification.',
             'Unit prices, copays, reimbursement settings, program and detailed schedule remain not verified: no verified read-only detail screen is available.']
    if contracts:
        notes.append('Existing contract(s) found. Do not create another without reconciling the observed records.')
    if pra.reuse_required.value:
        notes.append('The PDF requires PRA reuse. No PRA creation or amendment was attempted.')
    if c_reason == 'ambiguous' or p_reason == 'ambiguous':
        notes.append('Multiple records are present without a unique source match; related comparisons remain not verified.')
    if c_reason == 'not_verified' or p_reason == 'not_verified':
        notes.append('No readable rows were found for a record list. Absence is not confirmed; review the existing local LCW window.')
    if c_reason == 'missing' or p_reason == 'missing':
        notes.append('A relevant record list was confirmed empty. No new record was created; resolve the missing record manually.')
    return {'identity': identity, 'contracts': contracts, 'assignments': assignments,
            'contract_selection': c_reason, 'assignment_selection': p_reason,
            'coverage': coverage, 'comparison': compare_fields(source, observed), 'notes': notes}
