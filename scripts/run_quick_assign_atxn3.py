#!/usr/bin/env python3
"""Run quick pathway assignment on existing ATXN3 interactions."""

import sys
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import app
from models import Interaction
from scripts.pathway_v2.run_pipeline import run_pathway_pipeline

with app.app_context():
    ids = [i.id for i in Interaction.query.filter_by(discovered_in_query="ATXN3").all()]
    print(f"Found {len(ids)} ATXN3 interactions", file=sys.stderr)

    result = run_pathway_pipeline(quick_assign=True, interaction_ids=ids)

    print(f"\nResult: {result['status']}")
    print(f"Steps: {result['steps_completed']}")
    print(f"Timing: {result['timing']}")
    if result.get("quick_assign"):
        qa = result["quick_assign"]
        print(f"Matched existing: {qa.get('matched_existing', '?')}")
        print(f"Created new: {qa.get('created_new', '?')}")
