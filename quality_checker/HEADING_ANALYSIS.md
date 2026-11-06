# SATVIQ — Resume Section Heading Analysis & Alias Dictionary Evidence

How the alias additions in `config/completeness.yaml` were derived, and
how the updated detector was validated. All frequencies below are
measured, not estimated.

## 1. Corpus — read this first

**The ~100-resume dataset referenced in the task was not attached to
the request.** The analysis therefore ran on every real resume
legitimately available in the project:

- 33 stored SATVIQ records (real resumes previously processed by the
  system, across three builds: 24 + 4 + 5 records), and
- 8 loose resume files (6 of the owner's own resume PDFs,
  `sample_resume.pdf`, 1 DOCX resume),

deduplicated by extracted-text hash → **41 unique real resumes**
(PDF and DOCX sources). Method: heading-like lines (short, ≤ 8 words,
uppercase/title-case or colon-terminated, no email/URL/digit-heavy
lines) were extracted from each resume's text, normalised with the
filter's own `_normalise_heading()`, tallied across the corpus, and
then classified by hand against the content that followed each
heading. 739 unique heading-like lines were found; 159 carried
section vocabulary and were individually reviewed; the rest were
names, job titles, employers, project titles and skill lines.

**If the 100-CV dataset is provided, the same pipeline re-runs and
only the frequencies/evidence in this report change — the dictionary
structure and code do not.**

## 2. Canonical sections (unchanged)

7 scored sections, importances and penalties untouched:
Contact Information (required, 25), Summary (optional, 3),
Skills (recommended, 12), Work Experience (recommended, 12),
Education (recommended, 12), Projects (optional, 3),
Certifications (optional, 3).

Totals after this change: **83 strong aliases, 7 conditional
aliases** across the 7 sections, plus **9 observed-but-unscored
sections (21 aliases)** recorded in the YAML for future decisions.

## 3. Strong aliases added (heading match is sufficient)

Confidence is High for all rows: the phrase denotes the section
itself and detection requires a whole-line (or delimiter) match.

### Skills (+14)
| Original heading | Freq | Observed in |
|---|---|---|
| Expertise | 2 | SHJ-5354_SHC-350771.pdf, download_8_.pdf |
| RELEVANT SKILLS | 2 | SHJ-5354_SHC-299913.pdf, download_6_.pdf |
| Skills Summary | 1 | download_8_.pdf |
| Skillset | 1 | download_6_.pdf |
| IT Skills | 1 | download_7_.pdf |
| SOFTWARE & TOOLS | 1 | SHJ-5329_SHC-363888.pdf |
| Tools & Technologies | 1 | SHJ-5354_SHC-287405.pdf |
| TECHNICAL ENVIRONMENT | 1 | Satish_Mehta.docx |
| CORE SKILLS & COMPETENCIES | 1 | SHJ-5354_SHC-301275.pdf |
| CORE COMPETENCIES & PROFESSIONAL STRENGTHS | 1 | SHJ-5354_SHC-301275.pdf |
| CORE INFRASTRUCTURE COMPETENCIES | 1 | Satish_Mehta.docx |
| Communication Skills | 1 | download_10_.pdf |
| Interpersonal Skills | 1 | download_9_.pdf |
| Editing Skill | 1 | SHJ-5329_SHC-365545.pdf |

### Work Experience (+2)
| Original heading | Freq | Observed in |
|---|---|---|
| 🛠 Previous Experience | 1 | download_7_.pdf |
| FREELANCE EXPERIENCE | 1 | SHJ-5329_SHC-363912.pdf |

### Education (+2)
| Original heading | Freq | Observed in |
|---|---|---|
| ACADEMIC QUALIFICATION (singular) | 2 | SHJ-5451_SHC-364405.pdf, SHJ-5451_SHC-364510.pdf |
| EDUCATION & CERTIFICATIONS / EDUCATION AND CERTIFICATIONS | 2 | SHJ-5329_SHC-363912.pdf, SHJ-5354_SHC-353993.docx |

The combined heading is listed under **both** Education and
Certifications, so one line detects both sections.

### Projects (+2 corpus, +7 owner-specified)
| Original heading | Freq | Source |
|---|---|---|
| SELECT PROJECTS | 1 | SHJ-5329_SHC-365388.pdf (differs from existing "Selected Projects") |
| FLAGSHIP PROJECTS | 1 | SHJ-5354_SHC-368909.pdf |
| PORTFOLIO & WORK SAMPLES | 0 in corpus | owner-specified (the originally reported case) |
| Work Samples, Selected Works, Project Portfolio, Projects & Portfolio, Key Projects & Achievements, Relevant Projects | 0 in corpus | owner-specified |

### Certifications (+3)
| Original heading | Freq | Observed in |
|---|---|---|
| CERTIFICATIONS & TRAINING | 1 | SHJ-5354_SHC-245975.pdf |
| CERTIFICATIONS & TECHNICAL TRAINING | 1 | SHJ-5412_SHC-381369.pdf |
| EDUCATION AND CERTIFICATIONS (combined, shared with Education) | 2 | see Education |

## 4. Conditional aliases added (content validation required)

A conditional heading counts only if ≥ 8 words of substantive
content follow it (URLs, emails, phone numbers and link-item lines
excluded; window ends at the next recognised heading, max 15 lines).
Rejections are recorded in the result metrics as
`conditional_rejected`; acceptances carry
`detection_method: "heading_conditional"`, confidence 0.85.

| Alias | Section | Evidence |
|---|---|---|
| Portfolio | Projects | owner-specified; can be a bare link label |
| Professional Portfolio | Projects | owner-specified |
| Technical Portfolio | Projects | owner-specified |
| Portfolio & Website | Projects | corpus ×1 (SHJ-5329_SHC-365423.pdf) — a contact-block label there; the gate correctly **rejects** it |
| Tech Stack Projects | Projects | corpus ×1 (download_8_.pdf) — genuine projects section; gate **accepts** |
| Tools | Skills | corpus ×1 (SHJ-5329_SHC-365423.pdf) — followed by a real tool list |
| Academic Highlights | Education | corpus ×1 (SHJ-5354_SHC-301275.pdf) — final-year project + workshops |

## 5. Observed but NOT scored (recorded in YAML only)

These sections are real and frequent, but adding them to scoring was
explicitly out of scope. They live under
`observed_unscored_sections:` in `completeness.yaml`, which the
filter does not score — though their headings do terminate
conditional content windows.

| Section | Aliases (freq) |
|---|---|
| Languages | Languages ×6, Language ×3, Languages Known ×1, Language Known ×1, Languages & Interests ×1 |
| Achievements | Achievements ×6, Key Achievements ×4 |
| Awards | Awards ×1, Awards & Recognition ×1, Honor & Award ×1 |
| Publications | Publications & Research ×1 |
| Volunteer Experience | Volunteering & Community Engagement ×1 |
| Interests | Hobbies & Interests ×2, Strength & Hobby ×2, Interests ×1, Hobbies ×1, Languages & Interests ×1 |
| Strengths | Strengths ×2, Strength & Hobby ×2 |
| Declaration | Declaration ×2 |
| Links | Links ×2 |

Caution recorded in the YAML: "Languages: Python, SQL" in tech
resumes denotes programming languages (Skills content), which is why
Languages must not be naively mapped to a scored section.

## 6. Ambiguous headings — deliberately NOT mapped

| Heading | Freq | Why not mapped |
|---|---|---|
| Additional Qualifications | 1 | Its content in the only occurrence (SHJ-5412_SHC-379418.pdf) was a Strengths list — mapping it to Education would be a false positive |
| Courses | 1 | In its only occurrence (SHJ-5329_SHC-365545.pdf) it headed education entries, not certifications |
| Brand Experience / Top Clients & Brand Experience | 1 / 1 | Client lists, not employment history |
| Project Deck | 1 | A deliverable reference, not a projects section |
| Details | 1 | Meaningless without context |
| Agile & Project Management Skills; Cloud & DevOps Skills (In Progress) | 1 / 1 | Sub-headings inside a Skills section, not top-level sections |
| Sign-off & Project Handover | 1 | A job-duty line inside work content |

## 7. Code changes (minimal, in `filters/completeness.py`)

1. **Conditional alias support** — new per-section
   `conditional_aliases` config, evaluated only when no strong alias
   matched, gated by `_content_gate_passes()`. Thresholds are
   configurable (`conditional_content.min_words`,
   `max_window_lines`).
2. **`&` delimiter fix** — the inline-content match
   (`Heading: content`) now applies the same `&` → `and`
   normalisation to the raw line as aliases receive. Previously
   `Projects & Achievements: …` could never match while
   `Projects and Achievements: …` could.
3. The heading search was factored into one helper used by both the
   strong and conditional paths; strong-match evidence, scores,
   flags and result fields are byte-identical to before.

## 8. Validation against the corpus (old dictionary → new)

Per-section detection counts across the 41 resumes:

| Section | Before | After |
|---|---|---|
| Contact Information | 41 | 41 |
| Summary | 30 | 30 |
| Skills | 29 | **36** |
| Work Experience | 35 | 35 |
| Education | 33 | **37** |
| Projects | 11 | **14** |
| Certifications | 15 | **19** |

Mean completeness score **87.5 → 91.2**. 15 resumes gained at least
one correctly detected section; **no resume lost any section**.

Conditional gate outcomes on the corpus: **accepted 1**
(Tech Stack Projects, download_8_.pdf — genuine);
**rejected 3** (Portfolio & Website in SHJ-5329_SHC-365423.pdf;
"Portfolio: Google Drive (link on request)" in the LINKS block of
SHJ-5329_SHC-363873.pdf and sample_resume.pdf).

Validation initially caught a real false positive: the two
"Portfolio: Google Drive…" labels passed an early gate version
because neighbouring link lines (Instagram handles, a Wixsite URL)
counted as "words". The gate was hardened — link-item lines and the
heading's own inline remainder no longer count as content — and the
final results above are from the hardened gate. Negative cases from
the specification are pinned as tests: `Portfolio:
https://example.com` → not Projects; `Experience in Python` prose →
not Work Experience; `Qualification: BCA` → not Work Experience;
unscored headings (Languages/Achievements/Declaration) change
neither detection nor score.

Tests: 39 new tests in
`quality_checker/tests/test_section_alias_dictionary.py`; full
project suite **352 passed, 4 skipped** (was 313 + 4), zero
regressions.

## 9. Remaining limitations

- Frequencies reflect 41 resumes, not 100. Rare heading families
  (e.g. Publications, References, Memberships) are under-sampled;
  References and Professional Memberships did not occur at all in
  this corpus and were therefore **not** added anywhere.
- Heading discovery is bounded by extraction quality: tab-separated
  headings (`PERSONAL\tPROJECTS`) normalise correctly, but heavily
  interleaved two-column extractions can hide headings entirely.
- The content gate is word-count based; a portfolio section whose
  content is mostly images/links with very short captions could be
  rejected. The rejection is always visible in
  `conditional_rejected` for manual review.

---

# Addendum — 95-resume dataset (CVs_1_be3h.zip)

The dataset referenced in the original task was provided afterwards:
99 files (90 PDF, 7 DOCX, 2 DOC). Text was extracted from all of
them (PyMuPDF / DOCX XML / LibreOffice for legacy DOC); 4 PDFs were
image-only with no extractable text, leaving **95 unique resumes**
(no duplicates). Same method as the main report: 1,700 heading-like
lines, 367 section-vocabulary candidates reviewed against content.

## Additions from this dataset (marked [cv100] in the YAML)

- **Summary +9 strong:** Personal Profile ×4, Career Summary ×2,
  Career Objectives ×2, Executive Summary, Personal Statement,
  Synopsis, Career Brief, About Myself, Job Objective ×1 each.
- **Skills +11 strong:** Key Skills ×3, Soft Skills ×3, Professional
  Skills ×2, Core Competencies & Technical Skills, Skills and
  Competencies, Key Skills & Competencies, Technical & Quantitative
  Skills, Software Skills, Software Exposure ×1 each, plus the
  combined Skills & Certifications / Certificates & Skills ×1 each
  (listed under both sections). **+1 conditional:** Technical Tools.
- **Work Experience +4 strong:** Organisational Experience,
  Post-Qualification Work Experience, Pre-Qualification Work
  Experience, Working Experience ×1 each. **+1 conditional:** Work
  Profile (heads a duties list).
- **Education +10 strong:** Educational Qualification (singular) ×6,
  Technical Qualification ×3, Professional Qualification ×2,
  Academics ×2, Education & Credentials, Education & Professional
  Qualifications, Academic & Professional Qualifications, Education
  Details, Education Qualification, Educational Accomplishments ×1
  each. **+1 conditional:** Academic Achievements.
- **Projects +2 strong:** Project Work, Internship & Academic
  Project ×1 each.
- **Certifications +12 strong:** Awards and Certifications ×2,
  Training & Certifications, Certifications & Workshops,
  Certifications & Professional Development, Certifications &
  Specialized Training, Certification (singular), Additional
  Certification, Certifications and Achievements, and two combined
  Achievements/Certifications/Extra-Curricular headings ×1 each.
  **+2 conditional:** Vocational Training, Technical Training.
- **Unscored:** References observed for the first time (Reference ×1)
  and added as a new unscored section, along with Extracurricular
  Activities (×3) and Leadership (×1); Strength (singular) ×3,
  Publications (bare) ×1, Area of Interest ×1 and others added to
  existing unscored sections.

Deliberately NOT added from this dataset: "Project" (singular) and
"Personal Project" (singular) — in context they are project *title*
lines, not section headings; "Responsibilities" ×4 and its variants
— sub-headings inside experience sections; "Additional
Qualifications" ×2 — one occurrence headed diplomas, the other a
Strengths list (still ambiguous); misspellings found in real
headings ("EMPLOYMENT HISTROY", "EDUCATIONAL QULIFICATIONS",
"WORKEXPRIENCE") — a dictionary is the wrong place to store typos;
"Course" ×5 — an education-table column header, not a section.

## Two detector improvements driven by this dataset

1. **Glued numbered headings.** "01EXPERIENCE" / "02EDUCATION"
   (numbering with no space, an extraction artifact) are now
   recognised: `_normalise_heading` strips digits glued to a word.
2. **Sidebar link-label guard.** SHJ-5329_SHC-363945.pdf carries a
   sidebar "PORTFOLIO" label above a Behance link; two-column
   extraction leaked the summary prose and the next section's dates
   into the gate window and an early gate accepted it. The gate now
   additionally requires work-record evidence (bullet, year, or
   titled entry) when the first line after a conditional heading is
   a link item — and the window correctly stops at the glued
   "01EXPERIENCE" heading. The label is now rejected; the case is a
   pinned regression test.

## Validation on this dataset (previous dictionary → this one)

| Section | Before | After |
|---|---|---|
| Contact Information | 93 | 93 |
| Summary | 66 | **73** |
| Skills | 61 | **71** |
| Work Experience | 73 | **76** |
| Education | 65 | **78** |
| Projects | 12 | **13** |
| Certifications | 24 | **37** |

Mean completeness **82.8 → 86.8** across 95 resumes; 31 resumes
gained at least one section; **none lost one**. Conditional gate:
accepted Vocational Training and Academic Achievements (genuine);
rejected all three portfolio link labels.

---

# Addendum 2 — Content-evidence patterns & glued/letter-spaced headings

Driven by a reported miss: SHJ-5464_SHC-385075.pdf (Pranay Deshmukh),
whose education is a **table** (Degree / Board / Institute / % / CGPI
/ Year) with no Education heading, and whose achievements heading is
glued ("ACADEMICACHIEVEMENTS"). A corpus-wide miss analysis followed:
every resume × every undetected section was scanned for content
evidence.

## Error classes found

1. **Heading-less tables/lists** — education as a table (1 full case,
   + several degree-name-only cases), credentials stated inline
   ("CFA LEVEL 2 Cleared", "Project Manager | PMP Certified").
2. **Label-form headings** — "Education level: ITI / Diploma" in the
   Apna-style trade CVs → added as an Education alias (the delimiter
   match handles the value after the colon).
3. **Glued headings** — "ACADEMICACHIEVEMENTS", "CAREEROBJECTIVE",
   "WORKEXPRIENCE", "PERSONALINFORMATION", "TECHNICALQUALIFICATION".
4. **Letter-spaced headings** — "E D U C A T I O N", "S K I L L S",
   "C A R E E R O B J E C T I V E", "P O R T F O L I O & W O R K
   S A M P L E S" throughout the Apna-generated CVs.
   Classes 3+4 are fixed by ONE code change: the exact heading match
   is now also space-insensitive (a line whose letters, spaces
   removed, equal the alias's letters). 29 heading detections across
   the two corpora come from this alone — all genuine.

## Content-evidence patterns added (YAML `patterns`, as proposed)

Patterns fire only when no heading matched, at confidence 0.8.

- **Education:** (a) the three table headers together — `degree`
  anchored with `institute` + `board` lookaheads (a leading
  lookahead form was measured O(n²)-slow on table-less texts and
  rejected); (b) degree/qualification names: PGDM, MBA, MCA, BCA,
  B.Com/M.Com, B.Tech/M.Tech, B.Sc/M.Sc, B.E./M.E., BBA, Bachelor,
  Master of/'s, Ph.D, Class XII/X, HSC, SSC, Diploma, CGPA, CGPI.
- **Certifications:** credential acronyms (CFA, PMP, CPA, FRM,
  CISSP, CISA, PRINCE2) + credential-shaped word forms only:
  "Certified X", "Certification in/from X", 'Certification: "X"'.
  Every pattern detection on both corpora was audited by hand:
  15 gains, all genuine (PMP ×4, Certified CCMP, "Certification in
  Oracle Aconex", 'Training & Certification "ML Professional"',
  degree names ×6; one further gain is an artifact of a stored
  record whose text embeds JSON). Two candidate patterns were
  **rejected after audit**: bare "certificate" (meant HSC/SSC school
  certificates and a "Certificate of Appreciation" award) and bare
  "certifications" (fired on a job-duty sentence, "documents and
  certifications" — now a pinned negative test).
- **No patterns for Projects / Work Experience / Skills / Summary:**
  the evidence does not support any. Of 82 project misses only one
  resume had "Project:" entry lines, and they were ambiguous
  fragments; experience misses carry no reliable keyword signature
  (employer words ≈ 0 in most); skills and summary are prose/list
  content where a keyword pattern would fire almost everywhere.
  These sections stay heading-based — recorded as a limitation, not
  papered over with weak patterns.

## Results on the 95-resume dataset (before → after this addendum)

| Section | Before | After |
|---|---|---|
| Summary | 73 | **80** |
| Skills | 71 | **77** |
| Work Experience | 76 | **80** |
| Education | 78 | **91** |
| Certifications | 37 | **43** |
| Projects | 13 | 14 |
| Contact Information | 93 | 93 |

Mean completeness **86.8 → 90.1**. The reported resume itself:
**82 → 94** — Education detected (glued ACADEMICACHIEVEMENTS via
the space-insensitive conditional match), Certifications detected;
only Summary and Projects remain missing, and both are genuinely
absent from that resume.

One existing test was adjusted:
`test_missing_optional_sections_cost_less_than_required` renamed a
Certifications heading but left "NASSCOM Certified Data Scientist"
in the content; under content-evidence semantics that text IS
certification evidence, so the fixture now neutralises the
credential wording as well. Its intent (genuinely-absent optional
sections cost little) is unchanged. Full suite: **381 passed,
4 skipped**.

---

# Addendum 3 — Embedded headings ("OJT /Work Experience at TATA MOTORS…")

Reported miss: SHJ-5451_SHC-364880.pdf carries the line
"OJT /Work Experience at TATA MOTORS LTD, Pantnagar :" — a genuine
Work Experience heading with a prefix and the employer on the same
line. Exact, delimiter, and joined-line matching all miss it. A
corpus scan for aliases embedded in longer lines found 51 candidate
lines; most were prose ("deliver projects on schedule", "develop my
skills"), but four genuine heading shapes recurred:

- **A. connector suffix** — "<x> <alias> at <Employer>" (the reported
  line; also "…with / in / of / for").
- **B. combined suffix** — "PROFESSIONAL EXPERIENCE & TRAINING".
- **C. duration suffix** — "WORKING EXPERIENCE ( 2.5+ Years )".
- **D. digit-glued prefix** — "2289936201423Work Experience" (an ID
  number fused to the heading by extraction).

Implemented as `find_embedded()` in completeness.py, running after
exact/delimiter matching and before conditional aliases and
patterns, recorded as `heading_embedded` at confidence 0.9. Guards:
multi-word aliases only; ≤ 2 words before the alias; ≤ 12 words on
the line; Shape A additionally requires a capitalised employer/place
after the connector (blocks "Improved work experience at retail
stores"); B caps the suffix at 4 words; C requires the suffix to be
only digits + duration words. Two companion fixes: the delimiter
branch now ignores leading symbol-font bullet glyphs (private-use
characters), which recovered "CAREER OBJECTIVE :-To work…" in
SHJ-5442_SHC-321887.pdf; and two Education aliases observed in the
dataset were added ("Education Qualifications", "Education and
Qualification").

Corpus result: exactly **4 embedded hits in 95 resumes, all
genuine** — the four shapes above. Detections: Work Experience
80 → **84**, Education 91 → **93**, Summary 80 → **81**; mean
completeness 90.1 → **90.9**. The reported resume: **73 → 85**
(PyMuPDF text), Work Experience detected with the OJT line as
evidence; Skills remains undetected there (its skills content is a
duty bullet list under the experience narrative — no heading, and
no reliable content signature) and Projects is genuinely absent.

Full suite: **388 passed, 4 skipped**.
