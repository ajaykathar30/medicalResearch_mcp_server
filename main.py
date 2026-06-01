import os
import re
import logging
from typing import Annotated

import httpx  # not used yet — you'll need it for search_trials / openFDA next
from pydantic import Field
from Bio import Entrez
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("medical-mcp")

mcp = FastMCP(
    "medical",
    host="0.0.0.0",
    port=int(os.getenv("PORT", 10000)),
)

# --- NCBI config (read from env so you don't commit secrets) ---
Entrez.email = os.getenv("NCBI_EMAIL", "ajaykathar30@gmail.com")  # required etiquette
Entrez.tool = "medical-mcp"
_api_key = os.getenv("NCBI_API_KEY", "")
if _api_key:                       # only set it if you actually have one
    Entrez.api_key = _api_key      # raises 3 -> 10 requests/sec


def _parse(record) -> dict:
    """Turn one Biopython-parsed PubmedArticle into a clean dict."""
    citation = record["MedlineCitation"]
    article = citation["Article"]

    pmid = str(citation["PMID"])
    title = str(article.get("ArticleTitle", "")).strip()

    # --- Abstract: may be absent, or split into labeled sections ---
    abstract = ""
    abs_node = article.get("Abstract", {}).get("AbstractText")
    if abs_node:
        parts = []
        for seg in abs_node:
            text = str(seg).strip()
            if not text:
                continue
            label = getattr(seg, "attributes", {}).get("Label")
            parts.append(f"{label}: {text}" if label else text)
        abstract = " ".join(parts).strip()

    # --- Authors: people (LastName/ForeName) or group (CollectiveName) ---
    authors = []
    for a in article.get("AuthorList", []):
        if "CollectiveName" in a:
            authors.append(str(a["CollectiveName"]))
        else:
            fore = a.get("ForeName") or a.get("Initials") or ""
            last = a.get("LastName") or ""
            name = f"{fore} {last}".strip()
            if name:
                authors.append(name)

    # --- Journal + year (year may only live inside a MedlineDate string) ---
    journal = ""
    year = None
    journal_node = article.get("Journal")
    if journal_node:
        journal = str(journal_node.get("Title", "")).strip()
        pubdate = journal_node.get("JournalIssue", {}).get("PubDate", {})
        year = pubdate.get("Year")
        if not year and pubdate.get("MedlineDate"):
            m = re.search(r"\d{4}", str(pubdate["MedlineDate"]))
            year = m.group() if m else None

    # --- DOI / PMCID live in PubmedData, keyed by IdType ---
    ids = {}
    for aid in record.get("PubmedData", {}).get("ArticleIdList", []):
        ids[aid.attributes.get("IdType")] = str(aid)

    # --- MeSH terms (handy for filtering + the trend/forecast layer) ---
    mesh = [str(mh["DescriptorName"]) for mh in citation.get("MeshHeadingList", [])]

    return {
        "pmid": pmid,
        "title": title,
        "abstract": abstract,
        "authors": authors,
        "journal": journal,
        "year": year,
        "doi": ids.get("doi"),
        "pmcid": ids.get("pmc"),
        "mesh_terms": mesh,
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
    }


@mcp.tool()
def search_literature(
    query: Annotated[
        str,
        Field(description="Search query. Plain text ('GLP-1 agonists for Alzheimer's') "
                          "or PubMed syntax with field tags / boolean operators "
                          "(e.g. 'asthma[mesh] AND 2023:2025[pdat]')."),
    ],
    max_results: Annotated[
        int, Field(description="Max number of papers to return (1-50).", ge=1, le=50)
    ] = 20,
) -> list[dict]:
    """Search the biomedical literature (PubMed) and return relevant papers with abstracts.

    Use this to find published research, reviews, and studies on a disease, drug, gene,
    mechanism, or any biomedical topic. It runs a PubMed search and returns the most
    relevant articles, each with title, abstract, authors, journal, year, and identifiers
    (PMID, DOI) so they can be cited.

    Returns a list of paper dicts (pmid, title, abstract, authors, journal, year, doi,
    pmcid, mesh_terms, url). Returns an empty list if nothing matches.
    """
    try:
        # 1. query -> PMIDs  (this is the "get IDs from the query" step)
        handle = Entrez.esearch(db="pubmed", term=query, retmax=max_results, sort="relevance")
        pmids = Entrez.read(handle)["IdList"]
        handle.close()
        if not pmids:
            return []

        # 2. PMIDs -> full records with abstracts (XML; Entrez.read parses it to dicts)
        handle = Entrez.efetch(db="pubmed", id=",".join(pmids), rettype="abstract", retmode="xml")
        records = Entrez.read(handle).get("PubmedArticle", [])
        handle.close()

        # 3. shape into clean dicts for the agent
        return [_parse(r) for r in records]

    except Exception as e:
        logger.exception("search_literature failed")
        # Surfaces a readable error to the agent instead of a raw stack trace.
        raise RuntimeError(f"PubMed search failed: {e}") from e




CT_API = "https://clinicaltrials.gov/api/v2/studies"


def _parse_trial(study: dict) -> dict:
    """Flatten one ClinicalTrials.gov v2 study object into a clean dict."""
    ps = study.get("protocolSection", {})
    ident = ps.get("identificationModule", {})
    status = ps.get("statusModule", {})
    design = ps.get("designModule", {})
    sponsor = ps.get("sponsorCollaboratorsModule", {})
    desc = ps.get("descriptionModule", {})
    conditions = ps.get("conditionsModule", {})
    arms = ps.get("armsInterventionsModule", {})
    elig = ps.get("eligibilityModule", {})

    nct_id = ident.get("nctId")
    return {
        "nct_id": nct_id,
        "title": ident.get("briefTitle"),
        "status": status.get("overallStatus"),
        "phases": design.get("phases", []),                       # e.g. ["PHASE3"]
        "study_type": design.get("studyType"),                    # INTERVENTIONAL / OBSERVATIONAL
        "enrollment": design.get("enrollmentInfo", {}).get("count"),
        "conditions": conditions.get("conditions", []),
        "interventions": [
            {"type": i.get("type"), "name": i.get("name")}
            for i in arms.get("interventions", [])
        ],
        "lead_sponsor": sponsor.get("leadSponsor", {}).get("name"),
        "start_date": status.get("startDateStruct", {}).get("date"),
        "completion_date": status.get("completionDateStruct", {}).get("date"),
        "summary": desc.get("briefSummary"),
        "eligibility": elig.get("eligibilityCriteria"),
        "sex": elig.get("sex"),
        "min_age": elig.get("minimumAge"),
        "max_age": elig.get("maximumAge"),
        "has_results": study.get("hasResults"),
        "url": f"https://clinicaltrials.gov/study/{nct_id}" if nct_id else None,
    }


@mcp.tool()
def search_trials(
    condition: Annotated[
        str | None,
        Field(description="Disease or condition, e.g. 'Alzheimer disease', 'obesity'."),
    ] = None,
    intervention: Annotated[
        str | None,
        Field(description="Drug, therapy, or intervention, e.g. 'semaglutide', 'CAR-T'."),
    ] = None,
    other_terms: Annotated[
        str | None,
        Field(description="Any other free-text search terms."),
    ] = None,
    sponsor: Annotated[
        str | None,
        Field(description="Sponsor or collaborator name, e.g. 'Novo Nordisk'."),
    ] = None,
    status: Annotated[
        list[str] | None,
        Field(description="Filter by recruitment status. Valid values include: "
                          "RECRUITING, NOT_YET_RECRUITING, ACTIVE_NOT_RECRUITING, "
                          "ENROLLING_BY_INVITATION, COMPLETED, TERMINATED, SUSPENDED, "
                          "WITHDRAWN. Pass a list to match any of them."),
    ] = None,
    max_results: Annotated[
        int, Field(description="Max number of trials to return (1-50).", ge=1, le=50)
    ] = 20,
) -> list[dict]:
    """Search ClinicalTrials.gov for clinical trials and return structured trial records.

    Use this to find clinical trials by condition, drug/intervention, sponsor, or free
    text, and to filter by recruitment status. Good for "what trials exist / are
    recruiting", competitive-intelligence ("who is running trials on X"), pipeline and
    phase analysis, and trial-matching questions.

    Provide at least one of condition / intervention / other_terms / sponsor. Returns a
    list of trial dicts (nct_id, title, status, phases, study_type, enrollment,
    conditions, interventions, lead_sponsor, dates, summary, eligibility, url). Returns
    an empty list if nothing matches.
    """
    params: dict = {
        "format": "json",
        "pageSize": max(1, min(max_results, 50)),
        "sort": "@relevance",  
    }
    if condition:
        params["query.cond"] = condition
    if intervention:
        params["query.intr"] = intervention
    if other_terms:
        params["query.term"] = other_terms
    if sponsor:
        params["query.spons"] = sponsor
    if status:
        params["filter.overallStatus"] = "|".join(status)  # pipe-delimited list

    try:
        resp = httpx.get(CT_API, params=params, timeout=30.0)
        resp.raise_for_status()
        studies = resp.json().get("studies", [])
        return [_parse_trial(s) for s in studies]
    except Exception as e:
        logger.exception("search_trials failed")
        raise RuntimeError(f"ClinicalTrials.gov search failed: {e}") from e

if __name__ == "__main__":
    # streamable-http for deployment (Render sets PORT). For local Claude Desktop
    # testing instead, use: mcp.run(transport="stdio")
    mcp.run(transport="streamable-http")