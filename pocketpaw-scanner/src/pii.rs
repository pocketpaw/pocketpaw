use once_cell::sync::Lazy;
use pyo3::prelude::*;
use pyo3::types::PyModule;
use regex::{Regex, RegexBuilder};
use std::collections::HashSet;

struct PiiPattern {
    regex: Regex,
    pii_type: &'static str,
}

#[derive(Clone)]
struct PiiMatch {
    pii_type: String,
    start: usize,
    end: usize,
    original: String,
}

fn compile(pattern: &str, case_insensitive: bool) -> Regex {
    let mut builder = RegexBuilder::new(pattern);
    builder.case_insensitive(case_insensitive);
    builder.build().expect("invalid pii regex")
}

static PII_PATTERNS: Lazy<Vec<PiiPattern>> = Lazy::new(|| {
    vec![
        PiiPattern {
            regex: compile(r"\b\d{3}-\d{2}-\d{4}\b", false),
            pii_type: "ssn",
        },
        PiiPattern {
            regex: compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b", false),
            pii_type: "email",
        },
        PiiPattern {
            regex: compile(
                r"\b\+?1?[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b",
                false,
            ),
            pii_type: "phone",
        },
        PiiPattern {
            regex: compile(r"\b\+\d{1,3}[-.\s]?\d{4,14}\b", false),
            pii_type: "phone",
        },
        PiiPattern {
            regex: compile(r"\b4\d{3}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b", false),
            pii_type: "credit_card",
        },
        PiiPattern {
            regex: compile(r"\b5[1-5]\d{2}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b", false),
            pii_type: "credit_card",
        },
        PiiPattern {
            regex: compile(r"\b3[47]\d{2}[-\s]?\d{6}[-\s]?\d{5}\b", false),
            pii_type: "credit_card",
        },
        PiiPattern {
            regex: compile(
                r"\b6(?:011|5\d{2})[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b",
                false,
            ),
            pii_type: "credit_card",
        },
        PiiPattern {
            regex: compile(
                r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b",
                false,
            ),
            pii_type: "ip_address",
        },
        PiiPattern {
            regex: compile(
                r"\b(?:born|dob|birthday|date of birth)\b.{0,20}\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",
                true,
            ),
            pii_type: "date_of_birth",
        },
    ]
});

#[pyfunction]
fn scan(text: &str) -> Vec<(String, usize, usize, String)> {
    if text.is_empty() {
        return Vec::new();
    }

    let mut matches: Vec<PiiMatch> = Vec::new();
    for pattern in PII_PATTERNS.iter() {
        for found in pattern.regex.find_iter(text) {
            matches.push(PiiMatch {
                pii_type: pattern.pii_type.to_string(),
                start: found.start(),
                end: found.end(),
                original: found.as_str().to_string(),
            });
        }
    }

    matches.sort_by(|left, right| right.start.cmp(&left.start).then(right.end.cmp(&left.end)));

    let mut seen_ranges: HashSet<(usize, usize)> = HashSet::new();
    let mut deduped: Vec<PiiMatch> = Vec::new();

    for pii_match in matches {
        let range = (pii_match.start, pii_match.end);
        if seen_ranges.insert(range) {
            deduped.push(pii_match);
        }
    }

    deduped.reverse();
    deduped
        .into_iter()
        .map(|pii_match| {
            (
                pii_match.pii_type,
                pii_match.start,
                pii_match.end,
                pii_match.original,
            )
        })
        .collect()
}

pub fn register(py: Python<'_>, parent: &Bound<'_, PyModule>) -> PyResult<()> {
    let submodule = PyModule::new(py, "pii_detect")?;
    submodule.add_function(wrap_pyfunction!(scan, &submodule)?)?;
    parent.add_submodule(&submodule)?;
    Ok(())
}
