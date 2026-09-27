// The adapter registry. Adding a vendor is one file plus one line here - no
// other module knows a vendor's name.

import type { VendorName } from "../types.ts"
import type { Vendor } from "./vendor.ts"
import { greenhouse } from "./greenhouse.ts"
import { ashby } from "./ashby.ts"
import { personio } from "./personio.ts"
import { lever } from "./lever.ts"
import { smartrecruiters } from "./smartrecruiters.ts"
import { workday } from "./workday.ts"

export type { BoardHint, Vendor } from "./vendor.ts"
export { firstMatch } from "./vendor.ts"

export const ADAPTERS: Record<VendorName, Vendor> = {
  greenhouse,
  ashby,
  personio,
  lever,
  smartrecruiters,
  workday,
}

export function adapterFor(vendor: string): Vendor | null {
  return (ADAPTERS as Record<string, Vendor>)[vendor] ?? null
}
