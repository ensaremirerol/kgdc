# CHR extraction conventions

- Terminology codes: "(LOINC: 8302-2)" after a measurement name -> the chr:Measurement
  (not the MeasurementProcess) gets chr:hasCode <https://loinc.org/8302-2>.
  "(SNOMED: 195662009)" -> chr:hasCode <http://snomed.info/id/195662009> on both the
  chr:ClinicalCondition and its chr:DiagnosticStatement.
- Units: every chr:Unit gets chr:hasCode <https://biomedit.ch/rdf/sphn-resource/ucum/{encoded}>
  where {encoded} replaces UCUM characters: "/" -> "per", "." -> "dot", "%" -> "percent",
  "{" -> "cbl", "}" -> "cbr", "[" -> "sbl", "]" -> "sbr", "*" -> "exp".
  Examples: mg/dL -> mgperdL, kg/m2 -> kgperm2, /min -> permin, {score} -> cblscorecbr, % -> percent.
- "a <name> measurement was performed on <patient>" on a date: the chr:MeasurementProcess
  has chr:hasPatient, chr:hasPerformedDate (that date) and chr:hasResult -> the chr:Measurement,
  which carries chr:hasMeasuredDate (same date), chr:hasQuantityValue and chr:hasUnit.
- The attending physician named in the visit header is the chr:hasPerformer of every
  measurement, evaluation and medication administration in that visit, and the
  chr:hasCareProvider of the visit.
- The status sentence ("recorded with status \"completed\"") -> chr:hasStatus on the process,
  one shared ex:status_<value> individual.
- The visit (chr:ClinicalVisit) links every process of the encounter via chr:hasProcedure.
- "A care plan was established that refers to follow-up monitoring ..." names no concrete
  procedure: create the chr:CarePlan with its label, link chr:hasMedicalProcedure only to an
  existing process it plausibly refers to (the evaluation), never invent a new procedure.
