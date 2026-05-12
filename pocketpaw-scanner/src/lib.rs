use pyo3::prelude::*;

mod classify;
mod injection;
mod pii;

#[pymodule]
fn _core(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    injection::register(py, m)?;
    pii::register(py, m)?;
    classify::register(py, m)?;
    Ok(())
}
