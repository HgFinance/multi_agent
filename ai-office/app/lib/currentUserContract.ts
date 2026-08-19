export interface AuthorizedBook {
  bookId: string;
  name: string;
}

export interface AuthorizedFund {
  fundId: string;
  roles: string[];
  /** ACTIVE books projected by the server for which this user may trade. */
  books: AuthorizedBook[];
}

export interface CurrentUserProfile {
  schemaVersion: "portfolio.current-user.v1";
  userId: string;
  displayName: string;
  status: "ACTIVE";
  funds: AuthorizedFund[];
  onboardingRequired: boolean;
}

export function parseCurrentUser(body: unknown): CurrentUserProfile {
  if (!body || typeof body !== "object") throw new Error("invalid_current_user_response");
  const value = body as Record<string, unknown>;
  if (value.schema_version !== "portfolio.current-user.v1" || typeof value.user_id !== "string") {
    throw new Error("invalid_current_user_response");
  }
  const funds = Array.isArray(value.funds)
    ? value.funds.map((item) => {
        if (!item || typeof item !== "object" || typeof (item as { fund_id?: unknown }).fund_id !== "string") {
          throw new Error("invalid_current_user_fund");
        }
        const row = item as { fund_id: string; roles?: unknown; books?: unknown };
        const seenBookIds = new Set<string>();
        const books = Array.isArray(row.books)
          ? row.books.map((book) => {
              if (!book || typeof book !== "object") throw new Error("invalid_current_user_book");
              const projected = book as { book_id?: unknown; name?: unknown };
              if (
                typeof projected.book_id !== "string" ||
                !projected.book_id.trim() ||
                typeof projected.name !== "string" ||
                !projected.name.trim() ||
                seenBookIds.has(projected.book_id)
              ) {
                throw new Error("invalid_current_user_book");
              }
              seenBookIds.add(projected.book_id);
              return { bookId: projected.book_id, name: projected.name.trim() };
            })
          : [];
        return {
          fundId: row.fund_id,
          roles: Array.isArray(row.roles) ? row.roles.filter((role): role is string => typeof role === "string") : [],
          books,
        };
      })
    : [];
  if (value.status !== "ACTIVE") throw new Error("current_user_is_not_active");
  return {
    schemaVersion: "portfolio.current-user.v1",
    userId: value.user_id,
    displayName: typeof value.display_name === "string" ? value.display_name : value.user_id,
    status: "ACTIVE",
    funds,
    onboardingRequired: value.onboarding_required === true || funds.length === 0,
  };
}
