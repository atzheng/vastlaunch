;;; vastlaunch.el --- Emacs interface to vastlaunch -*- lexical-binding: t -*-

;; Version: 0.1.0

;; Usage: M-x vastlaunch-list

;;; Code:

(require 'tabulated-list)
(require 'json)

(defgroup vastlaunch nil
  "Emacs interface to vastlaunch."
  :group 'tools)

(defcustom vastlaunch-executable "vastlaunch"
  "Path to the vastlaunch executable."
  :type 'string
  :group 'vastlaunch)

(defcustom vastlaunch-auto-refresh-interval 5
  "Seconds between automatic refreshes, or nil to disable."
  :type '(choice (integer :tag "Seconds") (const :tag "Disabled" nil))
  :group 'vastlaunch)

(defvar-local vastlaunch--refresh-timer nil
  "Timer for auto-refreshing the vastlaunch buffer.")

(defvar-local vastlaunch--refreshing nil
  "Non-nil when an async refresh is in flight.")

(defface vastlaunch-queued    '((t :foreground "yellow"))        "Queued job.")
(defface vastlaunch-launching '((t :foreground "cyan"))          "Launching job.")
(defface vastlaunch-running   '((t :foreground "green"))         "Running job.")
(defface vastlaunch-success   '((t :foreground "green" :weight bold)) "Successful job.")
(defface vastlaunch-failed    '((t :foreground "red"))           "Failed job.")
(defface vastlaunch-stopped   '((t :inherit shadow))             "Stopped job.")

(defun vastlaunch--rel-time (ts)
  "Return a human-readable relative time string for Unix timestamp TS."
  (if (null ts)
      "—"
    (let ((d (floor (- (float-time) ts))))
      (cond
       ((< d 60)    (format "%ds ago"  d))
       ((< d 3600)  (format "%dm ago"  (/ d 60)))
       ((< d 86400) (format "%dh ago"  (/ d 3600)))
       (t           (format "%dd ago"  (/ d 86400)))))))

(defun vastlaunch--status-face (status)
  "Return the face for STATUS string."
  (pcase status
    ("queued"    'vastlaunch-queued)
    ("launching" 'vastlaunch-launching)
    ("running"   'vastlaunch-running)
    ("success"   'vastlaunch-success)
    ("failed"    'vastlaunch-failed)
    ("stopped"   'vastlaunch-stopped)
    (_           'default)))

(defun vastlaunch--fetch-jobs ()
  "Run `vastlaunch list --json' and return parsed job list, or nil on error."
  (let ((output (shell-command-to-string
                 (format "%s list --json" vastlaunch-executable))))
    (condition-case err
        (json-parse-string output
                           :array-type  'list
                           :object-type 'alist
                           :null-object  nil
                           :false-object nil)
      (error
       (message "vastlaunch: parse error: %s" (cadr err))
       nil))))

(defun vastlaunch--fetch-jobs-async (callback)
  "Run `vastlaunch list --json' asynchronously and call CALLBACK with the job list."
  (let ((buf (generate-new-buffer " *vastlaunch-async*")))
    (make-process
     :name "vastlaunch-list"
     :buffer buf
     :command (list vastlaunch-executable "list" "--json")
     :noquery t
     :sentinel (lambda (proc _event)
                 (unless (process-live-p proc)
                   (let ((output (with-current-buffer buf (buffer-string))))
                     (kill-buffer buf)
                     (let ((jobs (condition-case err
                                     (json-parse-string output
                                                        :array-type  'list
                                                        :object-type 'alist
                                                        :null-object  nil
                                                        :false-object nil)
                                   (error
                                    (message "vastlaunch: parse error: %s" (cadr err))
                                    nil))))
                       (funcall callback jobs))))))))

(defun vastlaunch--entries (jobs)
  "Convert JOBS alist list to `tabulated-list-mode' entries."
  (mapcar
   (lambda (job)
     (let* ((job-id   (or (alist-get 'job_id      job) "?"))
            (name     (or (alist-get 'name        job) "—"))
            (status   (or (alist-get 'status      job) "?"))
            (iid      (let ((v (alist-get 'instance_id job)))
                        (if v (number-to-string (round v)) "—")))
            (host     (alist-get 'host job))
            (port     (alist-get 'port job))
            (host-str (if (and host port)
                          (format "%s:%s" host (round port))
                        "—"))
            (started  (vastlaunch--rel-time (alist-get 'started_at job)))
            (updated  (vastlaunch--rel-time (alist-get 'updated_at job)))
            (face     (vastlaunch--status-face status)))
       (list job-id
             (vector
              (propertize job-id 'face 'font-lock-constant-face)
              name
              (propertize status 'face face)
              iid
              host-str
              started
              updated))))
   jobs))

(defun vastlaunch--job-id-at-point ()
  "Return the job ID for the tabulated-list entry at point, or nil."
  (tabulated-list-get-id))

;;; Actions

(defun vastlaunch-refresh ()
  "Refresh the vastlaunch job list (non-blocking)."
  (interactive)
  (unless vastlaunch--refreshing
    (setq vastlaunch--refreshing t)
    (let ((buf (current-buffer)))
      (vastlaunch--fetch-jobs-async
       (lambda (jobs)
         (when (buffer-live-p buf)
           (with-current-buffer buf
             (setq vastlaunch--refreshing nil)
             (if (null jobs)
                 (message "vastlaunch: no jobs (or server unreachable)")
               (setq tabulated-list-entries (vastlaunch--entries jobs))
               (tabulated-list-print t)
               (message "vastlaunch: %d job(s)" (length jobs))))))))))

(defun vastlaunch-destroy ()
  "Destroy the job at point after confirmation."
  (interactive)
  (when-let ((job-id (vastlaunch--job-id-at-point)))
    (when (yes-or-no-p (format "Destroy job %s? " job-id))
      (shell-command (format "%s destroy %s" vastlaunch-executable job-id))
      (vastlaunch-refresh))))

(defun vastlaunch-logs ()
  "Show logs for the job at point."
  (interactive)
  (when-let ((job-id (vastlaunch--job-id-at-point)))
    (let ((buf (get-buffer-create (format "*vastlaunch-logs: %s*" job-id))))
      (with-current-buffer buf
        (let ((inhibit-read-only t))
          (erase-buffer)
          (insert (shell-command-to-string
                   (format "%s logs %s" vastlaunch-executable job-id)))
          (goto-char (point-min))))
      (display-buffer buf))))

(defun vastlaunch-follow-logs ()
  "Follow logs for the job at point (async)."
  (interactive)
  (when-let ((job-id (vastlaunch--job-id-at-point)))
    (async-shell-command
     (format "%s logs -f %s" vastlaunch-executable job-id)
     (format "*vastlaunch-logs: %s*" job-id))))

(defun vastlaunch-ssh ()
  "Open an SSH session to the instance for the job at point."
  (interactive)
  (when-let ((job-id (vastlaunch--job-id-at-point)))
    ;; Resolve to instance ID first (works for both server and direct mode)
    (let* ((iid-str (shell-command-to-string
                     (format "%s id %s" vastlaunch-executable job-id)))
           (iid (string-trim iid-str)))
      (if (string-empty-p iid)
          (message "vastlaunch: could not resolve instance ID for %s" job-id)
        (let ((default-directory "~"))
          (async-shell-command
           (format "%s ssh %s" vastlaunch-executable iid)
           (format "*vastlaunch-ssh: %s*" job-id)))))))

;;; Mode

(defvar vastlaunch-mode-map
  (let ((map (make-sparse-keymap)))
    (define-key map (kbd "g")   #'vastlaunch-refresh)
    (define-key map (kbd "d")   #'vastlaunch-destroy)
    (define-key map (kbd "l")   #'vastlaunch-logs)
    (define-key map (kbd "L")   #'vastlaunch-follow-logs)
    (define-key map (kbd "s")   #'vastlaunch-ssh)
    (define-key map (kbd "RET") #'vastlaunch-logs)
    (define-key map (kbd "q")   #'quit-window)
    map)
  "Keymap for `vastlaunch-mode'.")

(defun vastlaunch--start-timer ()
  "Start the auto-refresh timer for the current buffer."
  (vastlaunch--stop-timer)
  (when vastlaunch-auto-refresh-interval
    (setq vastlaunch--refresh-timer
          (run-with-timer vastlaunch-auto-refresh-interval
                          vastlaunch-auto-refresh-interval
                          (let ((buf (current-buffer)))
                            (lambda ()
                              (when (buffer-live-p buf)
                                (with-current-buffer buf
                                  (vastlaunch-refresh)))))))))

(defun vastlaunch--stop-timer ()
  "Cancel the auto-refresh timer for the current buffer."
  (when vastlaunch--refresh-timer
    (cancel-timer vastlaunch--refresh-timer)
    (setq vastlaunch--refresh-timer nil)))

(define-derived-mode vastlaunch-mode tabulated-list-mode "Vastlaunch"
  "Major mode for browsing vastlaunch jobs.

\\{vastlaunch-mode-map}"
  (setq tabulated-list-format
        [("JOB ID"   22 t)
         ("NAME"     20 t)
         ("STATUS"   10 t)
         ("INSTANCE"  9 t)
         ("HOST"     22 t)
         ("STARTED"  10 nil)
         ("UPDATED"  10 nil)])
  (setq tabulated-list-padding 1)
  (setq tabulated-list-sort-key (cons "UPDATED" nil))
  (tabulated-list-init-header)
  (add-hook 'kill-buffer-hook #'vastlaunch--stop-timer nil t))

;;;###autoload
(defun vastlaunch-list ()
  "Open the vastlaunch job list buffer."
  (interactive)
  (let ((buf (get-buffer-create "*vastlaunch*")))
    (with-current-buffer buf
      (vastlaunch-mode)
      (vastlaunch-refresh)
      (vastlaunch--start-timer))
    (switch-to-buffer buf)))

(provide 'vastlaunch)
;;; vastlaunch.el ends here
