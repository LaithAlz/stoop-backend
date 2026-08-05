export const supabaseConfigured = true;
export const supabase = {
  auth: {
    getSession: async () => ({ data: { session: { access_token: "tok", expires_at: 9999999999 } } }),
    signOut: async () => ({}),
    onAuthStateChange: () => ({ data: { subscription: { unsubscribe() {} } } }),
  },
} as any;
