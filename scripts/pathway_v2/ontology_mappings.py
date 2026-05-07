#!/usr/bin/env python3
"""
Ontology Mappings for Pathway Pipeline
=======================================
Standard biological pathway → ontology ID mappings (GO, KEGG, Reactome)
with fuzzy matching support.

Extracted from utils/pathway_assigner.py (V1) for reuse in the unified V2 pipeline.
"""

import re
from difflib import SequenceMatcher
from typing import Dict, Optional


# ═══════════════════════════════════════════════════════════════════════════════
# ONTOLOGY MAPPINGS - Standard biological pathways with their ontology IDs
# ═══════════════════════════════════════════════════════════════════════════════

ONTOLOGY_MAPPINGS: Dict[str, Dict[str, str]] = {
    # Autophagy-related
    "autophagy": {"id": "GO:0006914", "source": "GO", "canonical": "Autophagy"},
    "macroautophagy": {"id": "GO:0016236", "source": "GO", "canonical": "Macroautophagy"},
    "mitophagy": {"id": "GO:0000423", "source": "GO", "canonical": "Mitophagy"},
    "aggrephagy": {"id": "GO:0035973", "source": "GO", "canonical": "Aggrephagy"},
    "chaperone-mediated autophagy": {"id": "GO:0061684", "source": "GO", "canonical": "Chaperone-Mediated Autophagy"},

    # Protein degradation
    "ubiquitin-proteasome system": {"id": "GO:0010415", "source": "GO", "canonical": "Ubiquitin-Proteasome System"},
    "proteasome": {"id": "GO:0010498", "source": "GO", "canonical": "Proteasomal Degradation"},
    "ubiquitination": {"id": "GO:0016567", "source": "GO", "canonical": "Protein Ubiquitination"},
    "deubiquitination": {"id": "GO:0016579", "source": "GO", "canonical": "Protein Deubiquitination"},
    "er-associated degradation": {"id": "GO:0036503", "source": "GO", "canonical": "ER-Associated Degradation"},
    "erad": {"id": "GO:0036503", "source": "GO", "canonical": "ER-Associated Degradation"},

    # Signaling pathways (KEGG)
    "mtor signaling": {"id": "hsa04150", "source": "KEGG", "canonical": "mTOR Signaling"},
    "mtorc1": {"id": "hsa04150", "source": "KEGG", "canonical": "mTOR Signaling"},
    "mtorc2": {"id": "hsa04150", "source": "KEGG", "canonical": "mTOR Signaling"},
    "pi3k-akt signaling": {"id": "hsa04151", "source": "KEGG", "canonical": "PI3K-Akt Signaling"},
    "mapk signaling": {"id": "hsa04010", "source": "KEGG", "canonical": "MAPK Signaling"},
    "nf-kb signaling": {"id": "hsa04064", "source": "KEGG", "canonical": "NF-kB Signaling"},
    "nf-kappab": {"id": "hsa04064", "source": "KEGG", "canonical": "NF-kB Signaling"},
    "wnt signaling": {"id": "hsa04310", "source": "KEGG", "canonical": "Wnt Signaling"},
    "notch signaling": {"id": "hsa04330", "source": "KEGG", "canonical": "Notch Signaling"},
    "hedgehog signaling": {"id": "hsa04340", "source": "KEGG", "canonical": "Hedgehog Signaling"},
    "tgf-beta signaling": {"id": "hsa04350", "source": "KEGG", "canonical": "TGF-beta Signaling"},
    "hippo signaling": {"id": "hsa04390", "source": "KEGG", "canonical": "Hippo Signaling"},
    "jak-stat signaling": {"id": "hsa04630", "source": "KEGG", "canonical": "JAK-STAT Signaling"},
    "calcium signaling": {"id": "hsa04020", "source": "KEGG", "canonical": "Calcium Signaling"},
    "camp signaling": {"id": "hsa04024", "source": "KEGG", "canonical": "cAMP Signaling"},

    # Cell death pathways
    "apoptosis": {"id": "GO:0006915", "source": "GO", "canonical": "Apoptosis"},
    "programmed cell death": {"id": "GO:0012501", "source": "GO", "canonical": "Programmed Cell Death"},
    "necroptosis": {"id": "GO:0070266", "source": "GO", "canonical": "Necroptosis"},
    "pyroptosis": {"id": "GO:0070269", "source": "GO", "canonical": "Pyroptosis"},
    "ferroptosis": {"id": "GO:0097707", "source": "GO", "canonical": "Ferroptosis"},

    # Cell cycle
    "cell cycle": {"id": "GO:0007049", "source": "GO", "canonical": "Cell Cycle"},
    "cell division": {"id": "GO:0051301", "source": "GO", "canonical": "Cell Division"},
    "mitosis": {"id": "GO:0007067", "source": "GO", "canonical": "Mitosis"},
    "dna replication": {"id": "GO:0006260", "source": "GO", "canonical": "DNA Replication"},

    # DNA damage/repair
    "dna damage response": {"id": "GO:0006974", "source": "GO", "canonical": "DNA Damage Response"},
    "dna repair": {"id": "GO:0006281", "source": "GO", "canonical": "DNA Repair"},
    "homologous recombination": {"id": "GO:0035825", "source": "GO", "canonical": "Homologous Recombination"},
    "non-homologous end joining": {"id": "GO:0006303", "source": "GO", "canonical": "Non-Homologous End Joining"},
    "nucleotide excision repair": {"id": "GO:0006289", "source": "GO", "canonical": "Nucleotide Excision Repair"},
    "base excision repair": {"id": "GO:0006284", "source": "GO", "canonical": "Base Excision Repair"},

    # Stress responses
    "unfolded protein response": {"id": "GO:0030968", "source": "GO", "canonical": "Unfolded Protein Response"},
    "upr": {"id": "GO:0030968", "source": "GO", "canonical": "Unfolded Protein Response"},
    "er stress": {"id": "GO:0034976", "source": "GO", "canonical": "ER Stress Response"},
    "heat shock response": {"id": "GO:0009408", "source": "GO", "canonical": "Heat Shock Response"},
    "oxidative stress": {"id": "GO:0006979", "source": "GO", "canonical": "Oxidative Stress Response"},
    "hypoxia response": {"id": "GO:0001666", "source": "GO", "canonical": "Hypoxia Response"},

    # Protein quality control
    "protein folding": {"id": "GO:0006457", "source": "GO", "canonical": "Protein Folding"},
    "chaperone": {"id": "GO:0006457", "source": "GO", "canonical": "Protein Folding"},
    "proteostasis": {"id": "GO:0006457", "source": "GO", "canonical": "Proteostasis"},

    # Transcription
    "transcription": {"id": "GO:0006351", "source": "GO", "canonical": "Transcription"},
    "transcriptional regulation": {"id": "GO:0006355", "source": "GO", "canonical": "Transcriptional Regulation"},
    "chromatin remodeling": {"id": "GO:0006338", "source": "GO", "canonical": "Chromatin Remodeling"},
    "epigenetic regulation": {"id": "GO:0040029", "source": "GO", "canonical": "Epigenetic Regulation"},

    # Inflammation/Immune
    "inflammation": {"id": "GO:0006954", "source": "GO", "canonical": "Inflammatory Response"},
    "immune response": {"id": "GO:0006955", "source": "GO", "canonical": "Immune Response"},
    "innate immunity": {"id": "GO:0045087", "source": "GO", "canonical": "Innate Immune Response"},
    "cytokine signaling": {"id": "hsa04060", "source": "KEGG", "canonical": "Cytokine Signaling"},

    # Metabolism
    "glycolysis": {"id": "GO:0006096", "source": "GO", "canonical": "Glycolysis"},
    "oxidative phosphorylation": {"id": "GO:0006119", "source": "GO", "canonical": "Oxidative Phosphorylation"},
    "lipid metabolism": {"id": "GO:0006629", "source": "GO", "canonical": "Lipid Metabolism"},
    "amino acid metabolism": {"id": "GO:0006520", "source": "GO", "canonical": "Amino Acid Metabolism"},

    # Vesicle trafficking
    "endocytosis": {"id": "GO:0006897", "source": "GO", "canonical": "Endocytosis"},
    "exocytosis": {"id": "GO:0006887", "source": "GO", "canonical": "Exocytosis"},
    "vesicle trafficking": {"id": "GO:0016192", "source": "GO", "canonical": "Vesicle Transport"},
    "lysosomal degradation": {"id": "GO:0007041", "source": "GO", "canonical": "Lysosomal Degradation"},

    # Cytoskeleton
    "cytoskeleton organization": {"id": "GO:0007015", "source": "GO", "canonical": "Cytoskeleton Organization"},
    "actin dynamics": {"id": "GO:0030031", "source": "GO", "canonical": "Actin Cytoskeleton Organization"},
    "microtubule organization": {"id": "GO:0000226", "source": "GO", "canonical": "Microtubule Organization"},

    # Neuronal
    "neurodegeneration": {"id": "GO:0070997", "source": "GO", "canonical": "Neurodegeneration"},
    "synaptic signaling": {"id": "GO:0099536", "source": "GO", "canonical": "Synaptic Signaling"},
    "axon guidance": {"id": "GO:0007411", "source": "GO", "canonical": "Axon Guidance"},
    "neuronal development": {"id": "GO:0048666", "source": "GO", "canonical": "Neuronal Development"},
}


def normalize_pathway_name(name: str) -> str:
    """Normalize pathway name for matching (strip non-alphanumeric, lowercase)."""
    return re.sub(r'[^a-z0-9]', '', name.lower())


def find_ontology_match(pathway_name: str) -> Optional[Dict[str, str]]:
    """Find best ontology match for a pathway name using fuzzy matching.

    Returns dict with keys {id, source, canonical} or None.
    """
    normalized = normalize_pathway_name(pathway_name)

    # Direct match
    if normalized in ONTOLOGY_MAPPINGS:
        return ONTOLOGY_MAPPINGS[normalized]

    # Substring match
    for key, mapping in ONTOLOGY_MAPPINGS.items():
        if key in normalized or normalized in key:
            return mapping

    # Fuzzy match (70% threshold)
    best_ratio = 0.0
    best_match = None
    for key, mapping in ONTOLOGY_MAPPINGS.items():
        ratio = SequenceMatcher(None, normalized, key).ratio()
        if ratio > best_ratio and ratio > 0.7:
            best_ratio = ratio
            best_match = mapping

    return best_match


def enrich_pathway_with_ontology(pathway_name: str) -> Optional[Dict[str, str]]:
    """Try to map a pathway name to a standard ontology entry.

    Returns dict with {ontology_id, ontology_source, canonical_name} or None.
    """
    match = find_ontology_match(pathway_name)
    if match is None:
        return None
    return {
        "ontology_id": match["id"],
        "ontology_source": match["source"],
        "canonical_name": match["canonical"],
    }
