"""
TrueWriting Analyzer - CLI Runner

Usage:
    py run.py --outlook                    (all accounts)
    py run.py --outlook --account gmail    (just gmail)
    py run.py --outlook --account gmail --account LifeStages
    py run.py --pst "path/to/file.pst"
    py run.py --mbox "path/to/sent.mbox"
    py run.py --eml-dir "path/to/eml_files"
    py run.py --outlook --months 6
"""

import argparse
import json
import os
import sys
from datetime import datetime

from analyzer import EmailIngester, TrueWritingAnalyzer


def main():
    parser = argparse.ArgumentParser(
        description="TrueWriting TW-0 Profile Generator"
    )
    parser.add_argument('--outlook', action='store_true', help='Read from live Outlook (must be running)')
    parser.add_argument('--account', action='append', help='Filter to specific Outlook account(s)')
    parser.add_argument('--pst', type=str, help='Path to .pst file')
    parser.add_argument('--mbox', type=str, help='Path to .mbox file')
    parser.add_argument('--eml-dir', type=str, help='Path to directory of .eml files')
    parser.add_argument('--months', type=int, default=12, help='Months back to analyze (default: 12)')
    parser.add_argument('--output', type=str, default=None, help='Output JSON filename')

    args = parser.parse_args()

    if not args.outlook and not args.pst and not args.mbox and not args.eml_dir:
        parser.error("Provide --outlook, --pst, --mbox, or --eml-dir")

    print("=" * 60)
    print("  TrueWriting Analyzer v0.3.0")
    print("  TW-0 Writing DNA Profile Generator")
    print("=" * 60)
    print()

    if args.outlook:
        print(f"Reading from live Outlook...")
        print(f"Analyzing last {args.months} months of sent email...")
        if args.account:
            print(f"Filtering to accounts: {', '.join(args.account)}")
        print()
        messages = EmailIngester.from_outlook_live(
            accounts=args.account, months_back=args.months
        )
    elif args.pst:
        if not os.path.exists(args.pst):
            print(f"ERROR: File not found: {args.pst}")
            sys.exit(1)
        print(f"Reading PST: {args.pst}")
        print(f"(Outlook will open briefly to read the PST)")
        messages = EmailIngester.from_pst(args.pst, months_back=args.months)
    elif args.mbox:
        if not os.path.exists(args.mbox):
            print(f"ERROR: File not found: {args.mbox}")
            sys.exit(1)
        print(f"Reading mbox: {args.mbox}")
        messages = EmailIngester.from_mbox(args.mbox, months_back=args.months)
    else:
        if not os.path.isdir(args.eml_dir):
            print(f"ERROR: Directory not found: {args.eml_dir}")
            sys.exit(1)
        print(f"Reading .eml files from: {args.eml_dir}")
        messages = EmailIngester.from_eml_directory(args.eml_dir, months_back=args.months)

    if not messages:
        print("\nERROR: No emails found to analyze.")
        print("  - For --outlook: make sure Outlook is running")
        print("  - Try increasing --months (default is 12)")
        sys.exit(1)

    print(f"\nFound {len(messages)} sent emails to analyze.\n")

    # Run analysis
    analyzer = TrueWritingAnalyzer(messages)
    profile = analyzer.analyze()

    # Save output
    output_file = args.output or f"tw0_profile_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), output_file)

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(profile, f, indent=2, ensure_ascii=False, default=str)

    # Print summary
    print()
    print("=" * 60)
    print("  TW-0 PROFILE SUMMARY")
    print("=" * 60)

    stats = profile["corpus_stats"]
    print(f"  Emails analyzed:     {stats['total_messages_analyzed']}")
    print(f"  Total words:         {stats['total_words']:,}")
    print(f"  Avg words/email:     {stats['avg_words_per_email']}")
    print()

    tone = profile["tone_indicators"]
    print(f"  Formality:           {tone['formality_label']} ({tone['baseline_formality']}/10)")
    print(f"  Energy:              {tone['energy']}")
    print(f"  Perspective:         {tone['perspective']}")
    print()

    vocab = profile["vocabulary"]
    print(f"  Vocabulary level:    {vocab['vocabulary_level']}")
    read = profile["readability"]
    print(f"  Readability:         {read.get('difficulty_label', 'n/a')} (grade {read.get('flesch_kincaid_grade', 'n/a')})")
    print(f"  Contraction style:   {profile['grammar_signature']['contraction_style']}")
    print(f"  Paragraph style:     {profile['paragraph_structure'].get('style', 'n/a')}")
    print()

    fp = profile["phrase_fingerprint"]

    sig_phrases = fp.get("signature_phrases", [])
    print(f"  === PHRASE FINGERPRINT ===")
    print(f"  Signature phrases:   {len(sig_phrases)}")
    if sig_phrases:
        print("  Top 10:")
        for p in sig_phrases[:10]:
            print(f'    "{p["phrase"]}" -- {p["frequency"]}x')
    print()

    templates = fp.get("sentence_templates", [])
    if templates:
        print(f"  Sentence templates:  {len(templates)}")
        for t in templates[:5]:
            print(f'    "{t["template"]}..." -- {t["frequency"]}x')
        print()

    greetings = fp.get("greeting_expressions", [])
    if greetings:
        print(f"  Greeting patterns:")
        for g in greetings[:5]:
            print(f'    "{g["greeting_pattern"]}" -- {g["frequency"]}x')
            if g.get("examples"):
                print(f'      e.g. "{g["examples"][0]}"')
        print()

    closings = fp.get("closing_expressions", [])
    if closings:
        print(f"  Closings used:")
        for c in closings[:5]:
            print(f'    "{c["closing"]}" -- {c["frequency"]}x')
        print()

    intens = fp.get("intensifiers_and_softeners", {})
    if intens:
        print(f"  Confidence style:    {intens.get('style', 'n/a')}")
        print(f"  Confidence ratio:    {intens.get('confidence_ratio', 'n/a')}")
        print()

    embedding_data = fp.get("phrase_embeddings_data", [])
    print(f"  Phrases ready for vector DB: {len(embedding_data)}")

    print()
    print(f"  Profile saved to: {output_path}")
    print(f"  Profile size: {os.path.getsize(output_path):,} bytes")
    print()
    print("=" * 60)


if __name__ == '__main__':
    main()
