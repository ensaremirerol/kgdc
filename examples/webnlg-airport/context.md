# WebNLG airport conventions

- The rdfs:label of every individual is the name exactly as the text writes it (the airport,
  the organisation, each place, country, person, aircraft, runway surface).
- "X is located in / situated in / found in Y" -> ap:location. "serves the city of Y" or
  "serves Y" -> ap:cityServed. A place's country ("Y, Spain", "in Spain") -> ap:country on
  the place. An organisation's headquarters city ("based in") -> ap:city on the organisation.
- Runway length and elevation are plain numbers as xsd:float (3500 -> "3500.0"^^xsd:float),
  without units. Runway designations ("14L/32R") and ICAO codes ("EGBF") are xsd:string.
- Runway surface types (Asphalt, Concrete, Gravel, ...) are ap:RunwaySurface individuals.
