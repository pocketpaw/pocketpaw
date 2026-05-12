use once_cell::sync::Lazy;
use pyo3::prelude::*;
use pyo3::types::PyModule;
use regex::{Regex, RegexBuilder};

fn case_insensitive(pattern: &str) -> Regex {
    RegexBuilder::new(pattern)
        .case_insensitive(true)
        .build()
        .expect("invalid content classifier regex")
}

struct Rule {
    name: &'static str,
    regex: Regex,
}

static SPAM_RULES: Lazy<Vec<Rule>> = Lazy::new(|| {
    vec![
        Rule {
            name: "marketing_urgency",
            regex: case_insensitive(r"\b(act now|limited time|offer expires|urgent)\b"),
        },
        Rule {
            name: "promotional_language",
            regex: case_insensitive(r"\b(click here|buy now|free money|guaranteed return)\b"),
        },
        Rule {
            name: "mass_outreach",
            regex: case_insensitive(r"\b(forward this|share with everyone|bulk message)\b"),
        },
    ]
});

static TOXIC_RULES: Lazy<Vec<Rule>> = Lazy::new(|| {
    vec![
        Rule {
            name: "abusive_language",
            regex: case_insensitive(r"\b(idiot|moron|stupid|worthless)\b"),
        },
        Rule {
            name: "hostile_intent",
            regex: case_insensitive(r"\b(i hate you|shut up|go away)\b"),
        },
        Rule {
            name: "self_harm_phrase",
            regex: case_insensitive(r"\b(kill yourself|hurt yourself)\b"),
        },
    ]
});

fn matched_rules(content: &str, rules: &[Rule]) -> Vec<String> {
    rules
        .iter()
        .filter(|rule| rule.regex.is_match(content))
        .map(|rule| rule.name.to_string())
        .collect()
}

#[pyfunction]
fn classify(content: &str) -> (String, f64, Vec<String>) {
    if content.trim().is_empty() {
        return ("safe".to_string(), 0.05, Vec::new());
    }

    let toxic_matches = matched_rules(content, TOXIC_RULES.as_slice());
    if !toxic_matches.is_empty() {
        let score = (0.6 + (toxic_matches.len() as f64 * 0.15)).min(0.99);
        return ("toxic".to_string(), score, toxic_matches);
    }

    let spam_matches = matched_rules(content, SPAM_RULES.as_slice());
    if !spam_matches.is_empty() {
        let score = (0.55 + (spam_matches.len() as f64 * 0.12)).min(0.95);
        return ("spam".to_string(), score, spam_matches);
    }

    ("safe".to_string(), 0.9, Vec::new())
}

pub fn register(py: Python<'_>, parent: &Bound<'_, PyModule>) -> PyResult<()> {
    let submodule = PyModule::new(py, "content_classify")?;
    submodule.add_function(wrap_pyfunction!(classify, &submodule)?)?;
    parent.add_submodule(&submodule)?;
    Ok(())
}
