import type { JestConfigWithTsJest } from 'ts-jest';

const jestConfig: JestConfigWithTsJest = {
  preset: 'ts-jest/presets/default-esm',
  moduleNameMapper: {
    '^convex/react$': '<rootDir>/convex/isol8/react.ts',
    '^convex/values$': '<rootDir>/convex/isol8/values.ts',
    '^convex/server$': '<rootDir>/convex/isol8/server.ts',
    '^convex/react-clerk$': '<rootDir>/convex/isol8/react.ts',
  },
};
export default jestConfig;
