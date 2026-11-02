# DQ feature test workbook

Upload `dq_feature_test.xlsx`, select every sheet at Phase A and accept it, paste `dq_feature_test_inputs.yaml` into the run inputs, then run Phase B. Rows below say what each sheet plants and what Phase B must show.

| Sheet | Planted | Phase B must show |
|---|---|---|
| City_Variants | Bangalore (6), BLR (3), Banglore (2) beside Bengaluru (20) | D-30 warn, 3 items, proposed Bengaluru; Accept fixes all cells of that spelling |
| City_Variants | Bombay (5), Mum (2), Mumbay (1) beside Mumbai (20) | D-30 warn, 3 items, proposed Mumbai |
| City_Variants | New Delhi (4), Dehli (2), Dilli (1) beside Delhi (18) | D-30 warn, 3 items, proposed Delhi |
| City_Variants | Hyd (3), Hyderbad (2); Madras (3); Calcutta (3); Poona (2) | D-30 warn, proposed Hyderabad / Chennai / Kolkata / Pune |
| City_Variants | Delhi NCR (3) | D-30 info: a known name that is not in the client's list; NOT merged into Delhi |
| City_Variants | Jaipur (6) is a real city but not in the client's list | D-30 info: 'a known city that is not in the project's city list' |
| City_Variants | Jaipure (1) | D-30 warn: a misspelling of Jaipur, which is not in the client's list; proposed Jaipur |
| City_Variants | Springfield (2), Qwertyville (2) | D-30 warn: not in the client's list, nothing proposed |
| City_Variants | Others (2), India (2) | NOT flagged: placeholders are skipped |
| Dates_Order | Meta / Facebook / Mumbai: dates ascending | no D-31 |
| Dates_Order | Google / Search / Delhi: newest first | counted as 'fall', not an error by itself |
| Dates_Order | Meta / Instagram / Pune: two rows swapped | D-31 warn: one item for the sheet, '2 of 4 series are not in date order', first place named (the sheet's other series are fine) |
| Dates_Order | Google / Display / Chennai: one row moved to the end | counted in the same D-31 item |
| Dates_NewestFirst | Every series runs newest first | D-31 info: all series are sorted newest first |
| Cols_Exact | Date, Platform, SubPlatform, City | no D-32 |
| Cols_Varied | Week Start, Channel, Sub-Platform, City Name | no D-32 (synonyms and longer headers) |
| Cols_Typo | Dat, Platfrom, SubPlatfrom, Cty | no D-32 (typos) |
| Cols_Missing | No sub-platform column | D-32: subplatform missing |
| Cols_Wrong | 'Sub Platform Name' only | D-32: platform missing; subplatform is found |
| Cols_AllMissing | Only 'Week' and 'Revenue' | D-32: platform, subplatform and city missing; date is found |
| Currency | Spend with 12 rupee-symbol values, 2 dollar and 1 euro | D-33 info (rupee, read as INR), D-33 warn for $ and for EUR; amounts unchanged, symbols removed |
| Column_Choice | Run inputs choose only Date, City, Spend; Remarks has padded text | Remarks passed on exactly as read; column_choice lists checked / not checked |

Review checks: answer a D-30 item with Accept in the review workbook and upload it; every cell with that spelling changes at once. Leave the client's city list out of the inputs to see the other mode (no list: unknown spellings are compared with each other).
