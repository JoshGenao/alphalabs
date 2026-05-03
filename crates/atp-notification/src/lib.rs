use atp_types::RuntimeService;

#[derive(Debug, Default)]
pub struct NotificationDispatcher;

impl NotificationDispatcher {
    pub fn service(&self) -> RuntimeService {
        RuntimeService::NotificationDispatcher
    }

    pub fn owns_operator_notifications(&self) -> bool {
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identifies_notification_dispatcher() {
        let dispatcher = NotificationDispatcher;
        assert_eq!(dispatcher.service(), RuntimeService::NotificationDispatcher);
        assert!(dispatcher.owns_operator_notifications());
    }
}
