import os
import re
import logging
from typing import Annotated
import json

import httpx  # not used yet — you'll need it for search_trials / openFDA next
from pydantic import Field
from Bio import Entrez
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("medical-mcp")



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


if __name__ == "__main__":
    # streamable-http for deployment (Render sets PORT). For local Claude Desktop
    # testing instead, use: mcp.run(transport="stdio")
    print(json.dumps(search_literature("GLP-1 agonists for Alzheimer's", 1), indent=2))