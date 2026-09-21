# ADE extraction conventions

- One ade:Drug per distinct drug name in the text; its rdfs:label is the drug name exactly as
  written (generic or brand, lower-case as in the text, no dose).
- An ade:AdverseEffect is the effect phrase exactly as the text names it: "ototoxicity",
  "hypercalcemia", "severe rash", "increased calcium-release". Keep the wording, do not
  normalise to a medical term the text does not use. One individual per distinct phrase.
- ade:causes links a drug to every adverse effect the text attributes to it ("X-induced Y",
  "Y caused by X", "Y after X", "Y associated with X", "X led to Y", "developed Y while on X").
- ade:dosage is the dose/regimen phrase for that drug copied verbatim ("1500 mg/m2",
  "4 times per day", "20 mg daily"); only when the text states one.
- Diseases the patient is treated FOR are not adverse effects.
- When the text names the same drug in several ways (methotrexate / MTX, 5-fluorouracil / 5-FU,
  cyclophosphamide / CP / cytoxan, NPH insulin / NPH), it is ONE ade:Drug with one rdfs:label per
  surface form used in the text.
- Every sign, symptom, laboratory finding or complication the patient developed on the drug is
  its own ade:AdverseEffect ("diffuse erythema", "pustules", "malaise", "fever up to 39 degrees C"),
  in addition to the overall diagnosis ("acute generalized exanthematous pustulosis"); link the
  drug to each of them.
