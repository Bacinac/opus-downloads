import { hr } from './hr';
import { en } from './en';
import { registerModule, type Word } from '$lib/opus';

export type MessageKey = keyof typeof hr | Word;

export const t = registerModule({ hr, en });
