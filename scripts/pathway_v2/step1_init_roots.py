#!/usr/bin/env python3
"""
Step 1: Initialize Hardcoded Root Pathways
==========================================
Enforces the existence of the 7 official Root (Level 0) pathways.
These are the ONLY allowable roots.

Roots:
1. Proteostasis
2. Metabolism & Bioenergetics
3. Membrane & Transport
4. Genome Maintenance
5. Gene Expression
6. Signal Transduction
7. Cytoskeletal Dynamics

These represent fundamental biological categories that organize all pathways.

Usage:
    python3 scripts/pathway_v2/step1_init_roots.py
"""

import sys
import logging
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Hardcoded Root Pathways (Level 0)
ROOT_PATHWAYS = [
    {"name": "Proteostasis", "ontology_id": "GO:0006457", "description": "Protein homeostasis mechanisms including folding, quality control, and degradation."},
    {"name": "Metabolism & Bioenergetics", "ontology_id": "GO:0008152", "description": "Chemical reactions involved in energy production and maintaining cellular functions."},
    {"name": "Membrane & Transport", "ontology_id": "GO:0016192", "description": "Membrane dynamics, vesicle trafficking, and cellular transport mechanisms."},
    {"name": "Genome Maintenance", "ontology_id": "GO:0006281", "description": "DNA repair, replication, chromatin organization, and genome stability."},
    {"name": "Gene Expression", "ontology_id": "GO:0010467", "description": "Transcription, translation, RNA processing, and gene regulation."},
    {"name": "Signal Transduction", "ontology_id": "GO:0007165", "description": "Cellular signaling cascades and signal transmission mechanisms."},
    {"name": "Cytoskeletal Dynamics", "ontology_id": "GO:0007010", "description": "Assembly, arrangement, and regulation of cytoskeletal structures."},
]

def init_roots():
    """Ensure all root pathways exist and are level 0."""
    try:
        from app import app, db
        from models import Pathway, PathwayParent
    except ImportError as e:
        logger.error(f"Failed to import app/db: {e}")
        return

    with app.app_context():
        logger.info("Checking Root Pathways...")
        
        # 1. Ensure all defined roots exist
        for root_def in ROOT_PATHWAYS:
            pathway = Pathway.query.filter_by(name=root_def['name']).first()
            if not pathway:
                logger.info(f"Creating NEW root: {root_def['name']}")
                pathway = Pathway(
                    name=root_def['name'],
                    ontology_id=root_def['ontology_id'],
                    ontology_source='GO',
                    description=root_def['description'],
                    hierarchy_level=0,
                    is_leaf=False,
                    ai_generated=False
                )
                db.session.add(pathway)
            else:
                # Update metadata if needed
                if pathway.hierarchy_level != 0:
                    logger.warning(f"Correcting hierarchy_level for {pathway.name} (was {pathway.hierarchy_level} -> 0)")
                    pathway.hierarchy_level = 0
                pathway.description = root_def['description']
                
                # Check for parents (Roots should NOT have parents in our strict tree)
                parents = PathwayParent.query.filter_by(child_pathway_id=pathway.id).all()
                if parents:
                    logger.warning(f"Root '{pathway.name}' has parents! Removing them to enforce Strict Tree.")
                    for p in parents:
                        db.session.delete(p)
                        
        db.session.commit()
        
        # 2. Check for ILLEGAL roots (everything else at level 0)
        valid_names = {r['name'] for r in ROOT_PATHWAYS}
        existing_roots = Pathway.query.filter_by(hierarchy_level=0).all()
        
        for p in existing_roots:
            if p.name not in valid_names:
                logger.warning(f"Found ILLEGAL ROOT: '{p.name}'. Demoting to Level 1 (Unknown Parent).")
                p.hierarchy_level = 1
                # We can't assign a parent automatically here, but we push it down so it's not a root.
                
        db.session.commit()
        logger.info("Root Pathway Initialization Complete.")

if __name__ == "__main__":
    init_roots()
