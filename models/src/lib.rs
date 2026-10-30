//! Model-neutral backend contract. Only a deterministic test backend is implemented.
#![forbid(unsafe_code)]
use serde::{Deserialize, Serialize};
use std::{
    collections::VecDeque,
    time::{Duration, Instant},
};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Request {
    pub task_id: String,
    pub input: String,
    pub max_tokens: u64,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Response {
    pub content: String,
    pub tokens: u64,
    pub backend: String,
}
#[derive(Debug, thiserror::Error)]
pub enum ModelError {
    #[error("backend failure: {0}")]
    Backend(String),
    #[error("request exceeded token budget")]
    Budget,
    #[error("backend timed out")]
    Timeout,
}
pub trait Backend {
    fn infer(&mut self, request: &Request) -> Result<Response, ModelError>;
}

#[derive(Debug, Default, Clone)]
pub struct DeterministicMock {
    pub delay: Duration,
    pub fail: bool,
    pub timeout: Option<Duration>,
}
impl Backend for DeterministicMock {
    fn infer(&mut self, request: &Request) -> Result<Response, ModelError> {
        if self.fail {
            return Err(ModelError::Backend("injected".into()));
        }
        if self.timeout.is_some_and(|t| self.delay > t) {
            return Err(ModelError::Timeout);
        }
        let tokens = request.input.split_whitespace().count() as u64 + 4;
        if tokens > request.max_tokens {
            return Err(ModelError::Budget);
        }
        std::thread::sleep(self.delay);
        Ok(Response {
            content: format!("# Verified test document\n\n{}\n", request.input),
            tokens,
            backend: "deterministic-mock-v1".into(),
        })
    }
}

pub struct FifoQueue<T = Request> {
    items: VecDeque<(T, Instant)>,
}
impl<T> Default for FifoQueue<T> {
    fn default() -> Self {
        Self {
            items: VecDeque::new(),
        }
    }
}
impl<T> FifoQueue<T> {
    pub fn push(&mut self, request: T) {
        self.items.push_back((request, Instant::now()));
    }
    /// Also used by the Core's durable shared scheduling admission path.
    pub fn pop_next(&mut self) -> Option<(Duration, T)> {
        self.items
            .pop_front()
            .map(|(r, queued)| (queued.elapsed(), r))
    }
    pub fn len(&self) -> usize {
        self.items.len()
    }
    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }
}
impl FifoQueue<Request> {
    pub fn run_next(
        &mut self,
        backend: &mut impl Backend,
    ) -> Option<(Duration, Result<Response, ModelError>)> {
        self.pop_next().map(|(wait, r)| (wait, backend.infer(&r)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn request(input: &str) -> Request {
        Request {
            task_id: "T".into(),
            input: input.into(),
            max_tokens: 100,
        }
    }
    #[test]
    fn deterministic_and_budgeted() {
        let mut b = DeterministicMock::default();
        let r = request("a b");
        assert_eq!(b.infer(&r).unwrap().content, b.infer(&r).unwrap().content);
        assert!(matches!(
            b.infer(&Request { max_tokens: 1, ..r }),
            Err(ModelError::Budget)
        ));
    }
    #[test]
    fn fifo_order_and_error() {
        let mut q = FifoQueue::default();
        q.push(request("first"));
        q.push(request("second"));
        let mut b = DeterministicMock::default();
        assert!(
            q.run_next(&mut b)
                .unwrap()
                .1
                .unwrap()
                .content
                .contains("first")
        );
        assert!(
            q.run_next(&mut b)
                .unwrap()
                .1
                .unwrap()
                .content
                .contains("second")
        );
        assert!(q.run_next(&mut b).is_none());
        b.fail = true;
        assert!(b.infer(&request("x")).is_err());
    }
    #[test]
    fn explicit_timeout() {
        let mut b = DeterministicMock {
            delay: Duration::from_secs(2),
            timeout: Some(Duration::from_millis(1)),
            fail: false,
        };
        assert!(matches!(b.infer(&request("x")), Err(ModelError::Timeout)));
    }
}
