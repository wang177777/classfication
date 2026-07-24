# JAMA Network Open literature endpoints

Europe PMC query for all JAMA Network Open records with first publication dates from 2026-01-24 through 2026-07-24:

https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=JOURNAL%3A%22JAMA%20Netw%20Open%22%20AND%20FIRST_PDATE%3A%5B2026-01-24%20TO%202026-07-24%5D&format=json&pageSize=1000&resultType=core

Crossref query for the same window:

https://api.crossref.org/journals/2574-3805/works?filter=from-pub-date%3A2026-01-24%2Cuntil-pub-date%3A2026-07-24%2Ctype%3Ajournal-article&rows=1000&select=DOI%2Ctitle%2Cauthor%2Cpublished%2Cpublished-online%2Cpublished-print%2Ctype%2Csubtype%2Cabstract%2CURL%2Clink%2Csubject%2Creference-count%2Cis-referenced-by-count
