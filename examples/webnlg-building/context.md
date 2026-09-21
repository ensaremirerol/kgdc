# WebNLG building conventions

- The rdfs:label of every individual is the name exactly as the text writes it.
- "designed by X" -> bd:architect (a Person). "owned by X" -> bd:owner. "the current tenant is X"
  -> bd:currentTenants; plain "tenant" -> bd:tenant. Owners and tenants are Organisations.
- "located on <street>" / a street address -> bd:address (string). "located in <city/town/state>"
  -> bd:location (a Place). A country named after a place ("Williamsburg, Virginia, United
  States") -> bd:country on that Place; a country stated for an organisation -> bd:country on it.
- Dates (construction start, completion) are xsd:string copied as written in the text.
- Architectural styles ("Modern architecture", "Gothic Revival") are bd:ArchitecturalStyle individuals.
- A place written with its state or country ("Williamsburg, Virginia", "Birmingham, Alabama") is ONE
  Place whose label is the whole phrase; its bd:country is only stated when the text names the country.
