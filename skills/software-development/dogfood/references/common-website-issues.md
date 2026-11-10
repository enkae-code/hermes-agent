# Common Website QA Patterns

Recurring issues found across multiple website QA sessions. Use this as a checklist during exploratory testing.

---

## Legal Compliance Issues

### Empty Privacy/Terms Pages

**Pattern:** Privacy Policy and Terms of Service pages exist but contain only a header and contact link, with no actual policy content.

**Risk:**
- Legal compliance violation (GDPR, CCPA, etc.)
- Unprofessional appearance
- Cannot enforce terms without published policy

**Detection:**
```bash
# Check if privacy/terms pages have substantial content
curl -s https://example.com/privacy | wc -w
# If < 200 words, likely placeholder
```

**Severity:** Critical for production sites

**Fix:** Work with legal counsel to add proper policy content, or use a template service like Termly/iubenda.

---

## Accessibility Issues

### Missing Reduced Motion Support

**Pattern:** Intro animations or auto-playing motion effects don't respect `prefers-reduced-motion`.

**Risk:** Motion-sensitive users (vestibular disorders, migraines) have poor experience

**Detection:**
- Check browser dev tools: Emulate CSS media feature `prefers-reduced-motion: reduce`
- Look for `@media (prefers-reduced-motion: reduce)` in CSS
- Check if JS checks `window.matchMedia('(prefers-reduced-motion: reduce)')`

**Example Fix:**
```tsx
// In animation component
const prefersReducedMotion = typeof window !== 'undefined'
  && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

if (prefersReducedMotion) {
  // Skip animation, show final state immediately
}
```

**Severity:** High (accessibility compliance)

---

## Form Validation Issues

### Missing Inline Validation Feedback

**Pattern:** Forms have labels but no real-time error messages. Users only find out about errors on submit.

**Risk:**
- Poor UX
- Frustration from repeated failed submissions
- Accessibility violation (error messages required for WCAG)

**Detection:**
- Fill form with invalid data
- Tab away from field
- Check if error message appears immediately
- If no feedback until submit, this issue exists

**Fix:** Use React Hook Form + Zod, or add `onBlur` validation with error state display.

**Severity:** Medium (UX/accessibility)

---

## Performance Issues

### No Loading States on CTAs

**Pattern:** Submit buttons and CTAs have no loading/disabled state during async operations.

**Risk:**
- Double-submissions
- Users click multiple times
- No feedback that action is processing

**Detection:**
- Click a submit button
- Check if button shows spinner or becomes disabled
- If button stays active during processing, this issue exists

**Fix:**
```tsx
const [isSubmitting, setIsSubmitting] = useState(false);

<button disabled={isSubmitting} onClick={handleSubmit}>
  {isSubmitting ? <Spinner /> : "Submit"}
</button>
```

**Severity:** Medium (UX)

---

## Content Issues

### Placeholder Content in Production

**Pattern:** Lorem ipsum, placeholder images, or "coming soon" text left in production.

**Detection:**
- Search for "Lorem ipsum", "coming soon", "placeholder"
- Look for stock images with watermarks
- Check for TODO/FIXME comments in production builds

**Severity:** Low-Medium (depends on visibility)

---

## Console Issues

### Next.js Scroll Behavior Warning

**Pattern:** Console warning about missing `data-scroll-behavior="smooth"` on `<html>`.

**Detection:**
```
Detected `scroll-behavior: smooth` on the `<html>` element.
In a future version, Next.js will no longer automatically disable
smooth scrolling during route transitions.
```

**Fix:** Add to `app/layout.tsx`:
```tsx
<html lang="en" data-scroll-behavior="smooth">
```

**Severity:** Low (future compatibility)

---

## Quick Checklist

During exploratory testing, verify:

- [ ] Privacy policy has actual content (not just header)
- [ ] Terms of service has actual content
- [ ] Intro animations respect `prefers-reduced-motion`
- [ ] Forms show inline validation errors
- [ ] Submit buttons have loading states
- [ ] No placeholder text in production
- [ ] No console errors on main pages
- [ ] 404 page is branded (not default Next.js)
- [ ] Favicon is set
- [ ] OG images exist for social sharing
